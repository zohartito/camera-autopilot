"""Bounded Sony ARW unpack and camera-colour linear render in a disposable Linux process.

File-size and TIFF dimension gates run in the parent before any native
allocation. The worker then installs RLIMIT_AS and may import LibRaw.
Crash or OOM is a typed error; the caller is not taken down. Linear render
applies as-shot white balance and the camera RGB→XYZ matrix; it does not
guess daylight WB. This is not stacking or a real-camera receipt.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import selectors
import signal
import stat
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_PIXELS = 40_000_000
ADDRESS_SPACE_BYTES = 2 * 1024 * 1024 * 1024
MAX_METADATA_BYTES = 64 * 1024
MAX_LINEAR_BYTES = MAX_PIXELS * 6 + 65536
BYTES_PER_UNPACKED_PIXEL = 8
MAX_IFDS = 32
MAX_IFD_ENTRIES = 128
_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8}


class IsolatedRawError(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


@dataclass(frozen=True)
class RawUnpackResult:
    width: int
    height: int
    visible_width: int
    visible_height: int
    dtype: str
    raw_sha256: str
    decoder: str
    elapsed_seconds: float
    address_space_limit: int
    unpacked: bool


@dataclass(frozen=True)
class RawLinearResult:
    width: int
    height: int
    dtype: str
    pixel_sha256: str
    camera_whitebalance: tuple[float, float, float]
    rgb_xyz_matrix: tuple[tuple[float, float, float], ...]
    colour_source: str
    decoder: str
    elapsed_seconds: float
    address_space_limit: int
    output_bytes: int


def _validate_limits(*, timeout_seconds: float, max_source_bytes: int, max_pixels: int,
                     address_space_bytes: int) -> None:
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or not 0.1 <= timeout_seconds <= 90:
        raise IsolatedRawError("invalid_limit", "Timeout must be between 0.1 and 90 seconds")
    if isinstance(max_source_bytes, bool) or not isinstance(max_source_bytes, int) or not 1024 <= max_source_bytes <= MAX_SOURCE_BYTES:
        raise IsolatedRawError("invalid_limit", "Source byte limit is invalid")
    if isinstance(max_pixels, bool) or not isinstance(max_pixels, int) or not 1 <= max_pixels <= MAX_PIXELS:
        raise IsolatedRawError("invalid_limit", "Pixel limit is invalid")
    if isinstance(address_space_bytes, bool) or not isinstance(address_space_bytes, int) or not 4096 <= address_space_bytes <= ADDRESS_SPACE_BYTES:
        raise IsolatedRawError("invalid_limit", "Address-space limit is invalid")


def _read_at(handle, offset: int, size: int, file_size: int) -> bytes:
    if size < 1 or offset < 0 or offset > file_size or size > file_size - offset:
        raise IsolatedRawError("truncated", "ARW header is truncated")
    handle.seek(offset)
    data = handle.read(size)
    if len(data) != size:
        raise IsolatedRawError("truncated", "ARW header is truncated")
    return data


def _entry_payload(handle, kind: int, count: int, slot: bytes, file_size: int) -> bytes | None:
    unit = _TYPE_SIZE.get(kind)
    if unit is None or count < 1 or count > 4096:
        return None
    nbytes = unit * count
    if nbytes > 4:
        return _read_at(handle, struct.unpack("<I", slot)[0], nbytes, file_size)
    return slot[:nbytes]


def _ascii(payload: bytes) -> str:
    return payload.split(b"\x00", 1)[0].decode("ascii", "replace")


def _scalar(payload: bytes, kind: int) -> int | None:
    if kind == 3 and len(payload) >= 2:
        return struct.unpack_from("<H", payload)[0]
    if kind == 4 and len(payload) >= 4:
        return struct.unpack_from("<I", payload)[0]
    return None


def _parse_tiff_bounds(handle, file_size: int, *, max_pixels: int, address_space_bytes: int) -> dict:
    header = _read_at(handle, 0, 8, file_size)
    if header[:3] == b"\xff\xd8\xff" or header[:8] == b"\x89PNG\r\n\x1a\n" or header[:4] == b"RIFF" or header[:3] == b"GIF":
        raise IsolatedRawError("not_raw", "File is not Sony ARW")
    if header[:4] != b"II*\x00":
        raise IsolatedRawError("not_raw", "File is not a little-endian TIFF Sony ARW")
    pending = [struct.unpack_from("<I", header, 4)[0]]
    seen: set[int] = set()
    max_width = max_height = 0
    make = model = ""
    sony_packed = False
    while pending:
        offset = pending.pop(0)
        if offset == 0:
            continue
        if offset in seen:
            raise IsolatedRawError("corrupt", "ARW IFD chain loops")
        seen.add(offset)
        if len(seen) > MAX_IFDS:
            raise IsolatedRawError("corrupt", "ARW has too many IFDs")
        count = struct.unpack("<H", _read_at(handle, offset, 2, file_size))[0]
        if count < 1 or count > MAX_IFD_ENTRIES:
            raise IsolatedRawError("corrupt", "ARW IFD is invalid")
        directory = _read_at(handle, offset + 2, 12 * count + 4, file_size)
        width = height = None
        for index in range(count):
            tag, kind, length, slot = struct.unpack_from("<HHI4s", directory, index * 12)
            payload = _entry_payload(handle, kind, length, slot, file_size)
            if payload is None:
                continue
            if tag == 256:
                width = _scalar(payload, kind)
            elif tag == 257:
                height = _scalar(payload, kind)
            elif tag == 259:
                sony_packed = sony_packed or _scalar(payload, kind) == 32767
            elif tag == 271 and kind == 2:
                make = _ascii(payload)
            elif tag == 272 and kind == 2:
                model = _ascii(payload)
            elif tag in {330, 34665} and kind == 4 and len(payload) >= 4:
                pending.extend(struct.unpack("<" + "I" * (len(payload) // 4), payload[: len(payload) - len(payload) % 4]))
        pending.append(struct.unpack_from("<I", directory, 12 * count)[0])
        if width is None and height is None:
            continue
        if width is None or height is None or width <= 0 or height <= 0:
            raise IsolatedRawError("corrupt", "ARW dimensions are invalid")
        if width > max_pixels or height > max_pixels or width * height > max_pixels:
            raise IsolatedRawError("dimension_limit", "ARW header claims dimensions beyond the pixel budget")
        if width * height > max_width * max_height:
            max_width, max_height = width, height
    if max_width < 1 or max_height < 1:
        raise IsolatedRawError("corrupt", "ARW header has no image dimensions")
    sony = make.upper().startswith("SONY") or model.upper().startswith("ILCE-7M") or sony_packed
    if not sony:
        raise IsolatedRawError("not_raw", "TIFF is not a Sony ARW")
    if max_width * max_height * BYTES_PER_UNPACKED_PIXEL > address_space_bytes:
        raise IsolatedRawError("memory_limit", "ARW unpack would exceed the address-space budget")
    return {"width": max_width, "height": max_height, "source_bytes": file_size,
            "make": make, "model": model}


def inspect_arw_header(path: str | Path, *, max_pixels: int = MAX_PIXELS,
                       max_source_bytes: int = MAX_SOURCE_BYTES,
                       address_space_bytes: int = ADDRESS_SPACE_BYTES) -> dict:
    path = Path(path)
    if path.suffix.lower() != ".arw":
        raise IsolatedRawError("not_raw", "Sony ARW decode requires a .arw path")
    if path.is_symlink():
        raise IsolatedRawError("path_rejected", "ARW path must not be a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise IsolatedRawError("path_rejected", "ARW could not be opened without following links") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise IsolatedRawError("path_rejected", "ARW must be a regular file")
        if not 0 < info.st_size <= max_source_bytes:
            raise IsolatedRawError("byte_limit", "ARW source is empty or exceeds the byte budget")
        return _parse_tiff_bounds(handle, info.st_size, max_pixels=max_pixels,
                                  address_space_bytes=address_space_bytes)


def _collect_worker(command: list[str], *, timeout_seconds: float, max_output: int) -> bytes:
    """Incremental bounded pipe reads; kill the worker group and reap on every failure."""
    deadline = time.monotonic() + timeout_seconds
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                               start_new_session=True, close_fds=True,
                               cwd=Path(__file__).resolve().parents[1])
    chunks = bytearray()
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise IsolatedRawError("timeout", "Isolated ARW decoder exceeded its hard deadline; worker terminated")
                for key, _ in selector.select(min(remaining, 0.1)):
                    chunk = os.read(key.fileobj.fileno(), min(65536, max_output + 1 - len(chunks)))
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        chunks.extend(chunk)
                        if len(chunks) > max_output:
                            raise IsolatedRawError("byte_limit", "Worker exceeded its output byte limit")
            remaining = deadline - time.monotonic()
            try:
                code = process.wait(timeout=max(0.001, remaining))
            except subprocess.TimeoutExpired as exc:
                raise IsolatedRawError("timeout", "Isolated ARW decoder exceeded its hard deadline") from exc
            if code:
                if code < 0 and -code in (signal.SIGKILL, signal.SIGSEGV, getattr(signal, "SIGBUS", 0)):
                    raise IsolatedRawError("memory_limit", "Decoder worker exceeded its address-space limit or was killed")
                raise IsolatedRawError("worker_failed", f"Decoder worker exited with status {code}; no unpack claimed")
        return bytes(chunks)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        process.stdout.close()


def _set_worker_limits(address_space_bytes: int, max_source_bytes: int, max_output_bytes: int = 0) -> None:
    import resource
    try:
        resource.setrlimit(resource.RLIMIT_AS, (address_space_bytes, address_space_bytes))
    except (OSError, ValueError) as exc:
        raise IsolatedRawError("unsupported_memory_limit",
            "This host cannot enforce the ARW decoder address-space limit") from exc
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (90, 91))
    file_bytes = max(max_source_bytes, max_output_bytes)
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_bytes, file_bytes))
    signal.alarm(91)


def _native_unpack(path: str, *, max_pixels: int, max_source_bytes: int, address_space_bytes: int) -> dict:
    inspect_arw_header(path, max_pixels=max_pixels, max_source_bytes=max_source_bytes,
                       address_space_bytes=address_space_bytes)
    try:
        import rawpy
    except ImportError as exc:
        raise IsolatedRawError("decoder_unavailable", "LibRaw via rawpy is not installed") from exc
    try:
        with rawpy.RawPy() as decoder:
            decoder.open_file(str(path))
            sizes = decoder.sizes
            raw_pixels = sizes.raw_width * sizes.raw_height
            vis_pixels = sizes.width * sizes.height
            if min(sizes.raw_width, sizes.raw_height, sizes.width, sizes.height) < 1:
                raise IsolatedRawError("corrupt", "LibRaw reported invalid ARW sizes")
            if max(raw_pixels, vis_pixels) > max_pixels:
                raise IsolatedRawError("dimension_limit", "LibRaw sizes exceed the pixel budget")
            decoder.unpack()
            image = decoder.raw_image
            if image.size > max_pixels:
                raise IsolatedRawError("dimension_limit", "Unpacked ARW exceeds the pixel budget")
            version = list(rawpy.libraw_version) if hasattr(rawpy, "libraw_version") else None
            return {
                "width": int(sizes.raw_width), "height": int(sizes.raw_height),
                "visible_width": int(sizes.width), "visible_height": int(sizes.height),
                "dtype": str(image.dtype), "raw_sha256": hashlib.sha256(image.tobytes()).hexdigest(),
                "decoder": "LibRaw via rawpy", "rawpy_version": getattr(rawpy, "__version__", None),
                "libraw_version": version, "unpacked": True,
            }
    except IsolatedRawError:
        raise
    except MemoryError as exc:
        raise IsolatedRawError("memory_limit", "LibRaw unpack exceeded the address-space limit") from exc
    except Exception as exc:
        raise IsolatedRawError("corrupt", "LibRaw rejected the ARW") from exc


def decode_arw_isolated(path: str | Path, *, timeout_seconds: float = 30.0,
                        max_source_bytes: int = MAX_SOURCE_BYTES, max_pixels: int = MAX_PIXELS,
                        address_space_bytes: int = ADDRESS_SPACE_BYTES) -> RawUnpackResult:
    _validate_limits(timeout_seconds=timeout_seconds, max_source_bytes=max_source_bytes,
                     max_pixels=max_pixels, address_space_bytes=address_space_bytes)
    header = inspect_arw_header(path, max_pixels=max_pixels, max_source_bytes=max_source_bytes,
                                address_space_bytes=address_space_bytes)
    if not sys.platform.startswith("linux"):
        raise IsolatedRawError("unsupported_memory_limit", "RAW requires hard address-space enforcement")
    started = time.monotonic()
    command = [sys.executable, str(Path(__file__).resolve()), "--worker", str(Path(path).resolve()),
               str(max_pixels), str(address_space_bytes), str(max_source_bytes)]
    raw = _collect_worker(command, timeout_seconds=timeout_seconds, max_output=MAX_METADATA_BYTES)
    line, separator, extra = raw.partition(b"\n")
    if not separator or extra or len(line) > MAX_METADATA_BYTES:
        raise IsolatedRawError("invalid_protocol", "Worker metadata is missing, trailing, or too large")
    try:
        metadata = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IsolatedRawError("invalid_protocol", "Worker metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise IsolatedRawError("invalid_protocol", "Worker metadata is not an object")
    if metadata.get("ok") is not True:
        raise IsolatedRawError(str(metadata.get("code", "worker_failed"))[:64],
                               str(metadata.get("error", "Isolated ARW unpack failed"))[:2000])
    width, height = metadata.get("width"), metadata.get("height")
    if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
        raise IsolatedRawError("invalid_protocol", "Worker dimensions are invalid")
    if min(width, height) < 1 or width * height > max_pixels or metadata.get("unpacked") is not True:
        raise IsolatedRawError("invalid_protocol", "Worker unpack result exceeds bounds")
    if width * height > header["width"] * header["height"] * 2:
        raise IsolatedRawError("invalid_protocol", "Worker dimensions exceed the header bound")
    return RawUnpackResult(
        width=metadata["width"], height=metadata["height"],
        visible_width=int(metadata.get("visible_width") or metadata["width"]),
        visible_height=int(metadata.get("visible_height") or metadata["height"]),
        dtype=str(metadata.get("dtype") or ""),
        raw_sha256=str(metadata.get("raw_sha256") or ""),
        decoder=str(metadata.get("decoder") or "LibRaw via rawpy"),
        elapsed_seconds=time.monotonic() - started,
        address_space_limit=address_space_bytes,
        unpacked=True,
    )


def _colour_from_decoder(decoder) -> tuple[list[float], list[list[float]]]:
    from raw_colour import require_camera_whitebalance, require_rgb_xyz_matrix
    try:
        whitebalance = require_camera_whitebalance(decoder.camera_whitebalance)
        matrix = require_rgb_xyz_matrix(decoder.rgb_xyz_matrix)
    except ValueError as exc:
        raise IsolatedRawError("colour_missing", str(exc)) from exc
    return whitebalance, matrix


def _native_linear(path: str, output: str, *, max_pixels: int, max_source_bytes: int,
                   address_space_bytes: int) -> dict:
    inspect_arw_header(path, max_pixels=max_pixels, max_source_bytes=max_source_bytes,
                       address_space_bytes=address_space_bytes)
    try:
        import rawpy
    except ImportError as exc:
        raise IsolatedRawError("decoder_unavailable", "LibRaw via rawpy is not installed") from exc
    from raw_colour import camera_rgb_to_linear_srgb, linear_tiff_bytes_for, write_linear_tiff
    try:
        with rawpy.RawPy() as decoder:
            decoder.open_file(str(path))
            sizes = decoder.sizes
            raw_pixels = sizes.raw_width * sizes.raw_height
            vis_pixels = sizes.width * sizes.height
            if min(sizes.raw_width, sizes.raw_height, sizes.width, sizes.height) < 1:
                raise IsolatedRawError("corrupt", "LibRaw reported invalid ARW sizes")
            if max(raw_pixels, vis_pixels) > max_pixels:
                raise IsolatedRawError("dimension_limit", "LibRaw sizes exceed the pixel budget")
            decoder.unpack()
            whitebalance, matrix = _colour_from_decoder(decoder)
            # Neutral user_wb: with use_camera_wb=False and use_auto_wb=False and no
            # user_wb, LibRaw still scales by its daylight pre_mul multipliers, so the
            # output would already be white-balanced. camera_rgb_to_linear_srgb applies
            # the as-shot camera WB itself, so force [1,1,1,1] here to keep the camera
            # channels neutral and apply white balance exactly once.
            camera_rgb = decoder.postprocess(
                output_bps=16, gamma=(1, 1), no_auto_bright=True,
                use_auto_wb=False, use_camera_wb=False, user_wb=[1.0, 1.0, 1.0, 1.0],
                output_color=rawpy.ColorSpace.raw)
            if camera_rgb.ndim != 3 or camera_rgb.shape[2] < 3:
                raise IsolatedRawError("corrupt", "LibRaw camera RGB is invalid")
            camera_rgb = camera_rgb[:, :, :3]
            height, width = int(camera_rgb.shape[0]), int(camera_rgb.shape[1])
            if min(width, height) < 1 or width * height > max_pixels:
                raise IsolatedRawError("dimension_limit", "Demosaiced ARW exceeds the pixel budget")
            if linear_tiff_bytes_for(width, height) > MAX_LINEAR_BYTES:
                raise IsolatedRawError("memory_limit", "Linear TIFF would exceed the file budget")
            linear = camera_rgb_to_linear_srgb(camera_rgb, whitebalance, matrix)
            write_linear_tiff(output, linear)
            version = list(rawpy.libraw_version) if hasattr(rawpy, "libraw_version") else None
            return {
                "width": width, "height": height, "dtype": "uint16",
                "pixel_sha256": hashlib.sha256(linear.tobytes()).hexdigest(),
                "camera_whitebalance": whitebalance, "rgb_xyz_matrix": matrix,
                "colour_source": "camera", "decoder": "LibRaw via rawpy; camera WB and RGB->XYZ",
                "rawpy_version": getattr(rawpy, "__version__", None),
                "libraw_version": version, "unpacked": True,
            }
    except IsolatedRawError:
        raise
    except MemoryError as exc:
        raise IsolatedRawError("memory_limit", "LibRaw linear render exceeded the address-space limit") from exc
    except Exception as exc:
        raise IsolatedRawError("corrupt", "LibRaw rejected the ARW") from exc


def _read_worker_metadata(raw: bytes) -> dict:
    line, separator, extra = raw.partition(b"\n")
    if not separator or extra or len(line) > MAX_METADATA_BYTES:
        raise IsolatedRawError("invalid_protocol", "Worker metadata is missing, trailing, or too large")
    try:
        metadata = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise IsolatedRawError("invalid_protocol", "Worker metadata is invalid") from exc
    if not isinstance(metadata, dict):
        raise IsolatedRawError("invalid_protocol", "Worker metadata is not an object")
    if metadata.get("ok") is not True:
        raise IsolatedRawError(str(metadata.get("code", "worker_failed"))[:64],
                               str(metadata.get("error", "Isolated ARW unpack failed"))[:2000])
    return metadata


def render_arw_linear_isolated(path: str | Path, output_tiff: str | Path, *, timeout_seconds: float = 30.0,
                               max_source_bytes: int = MAX_SOURCE_BYTES, max_pixels: int = MAX_PIXELS,
                               address_space_bytes: int = ADDRESS_SPACE_BYTES) -> RawLinearResult:
    from raw_colour import linear_tiff_bytes_for, require_camera_whitebalance, require_rgb_xyz_matrix
    _validate_limits(timeout_seconds=timeout_seconds, max_source_bytes=max_source_bytes,
                     max_pixels=max_pixels, address_space_bytes=address_space_bytes)
    inspect_arw_header(path, max_pixels=max_pixels, max_source_bytes=max_source_bytes,
                       address_space_bytes=address_space_bytes)
    output_tiff = Path(output_tiff)
    if output_tiff.suffix.lower() not in {".tiff", ".tif"}:
        raise IsolatedRawError("path_rejected", "Linear output must be a TIFF")
    if output_tiff.exists() or output_tiff.is_symlink():
        raise IsolatedRawError("path_rejected", "Linear output must be a new file")
    if not sys.platform.startswith("linux"):
        raise IsolatedRawError("unsupported_memory_limit", "RAW requires hard address-space enforcement")
    started = time.monotonic()
    command = [sys.executable, str(Path(__file__).resolve()), "--worker-linear",
               str(Path(path).resolve()), str(output_tiff.resolve()),
               str(max_pixels), str(address_space_bytes), str(max_source_bytes)]
    try:
        raw = _collect_worker(command, timeout_seconds=timeout_seconds, max_output=MAX_METADATA_BYTES)
        metadata = _read_worker_metadata(raw)
        width, height = metadata.get("width"), metadata.get("height")
        if isinstance(width, bool) or isinstance(height, bool) or not isinstance(width, int) or not isinstance(height, int):
            raise IsolatedRawError("invalid_protocol", "Worker dimensions are invalid")
        if min(width, height) < 1 or width * height > max_pixels or metadata.get("unpacked") is not True:
            raise IsolatedRawError("invalid_protocol", "Worker linear result exceeds bounds")
        if metadata.get("colour_source") != "camera":
            raise IsolatedRawError("invalid_protocol", "Worker did not use camera colour")
        try:
            whitebalance = require_camera_whitebalance(metadata.get("camera_whitebalance"))
            matrix = require_rgb_xyz_matrix(metadata.get("rgb_xyz_matrix"))
        except ValueError as exc:
            raise IsolatedRawError("invalid_protocol", "Worker colour metadata is invalid") from exc
        digest = str(metadata.get("pixel_sha256") or "")
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise IsolatedRawError("invalid_protocol", "Worker pixel hash is invalid")
        if output_tiff.is_symlink() or not output_tiff.is_file():
            raise IsolatedRawError("invalid_protocol", "Worker did not publish a linear TIFF")
        size = output_tiff.stat().st_size
        if size != linear_tiff_bytes_for(width, height):
            raise IsolatedRawError("invalid_protocol", "Linear TIFF size does not match dimensions")
        return RawLinearResult(
            width=width, height=height, dtype=str(metadata.get("dtype") or "uint16"),
            pixel_sha256=digest,
            camera_whitebalance=(whitebalance[0], whitebalance[1], whitebalance[2]),
            rgb_xyz_matrix=tuple(tuple(row) for row in matrix),
            colour_source="camera",
            decoder=str(metadata.get("decoder") or "LibRaw via rawpy; camera WB and RGB->XYZ"),
            elapsed_seconds=time.monotonic() - started,
            address_space_limit=address_space_bytes,
            output_bytes=size,
        )
    except Exception:
        output_tiff.unlink(missing_ok=True)
        raise


def _worker_main(argv: list[str]) -> int:
    try:
        path, max_pixels, address_space, max_source = argv
        _set_worker_limits(int(address_space), int(max_source))
        metadata = _native_unpack(path, max_pixels=int(max_pixels), max_source_bytes=int(max_source),
                                  address_space_bytes=int(address_space))
        header = json.dumps({"ok": True, **metadata}, separators=(",", ":")).encode()
        if len(header) > MAX_METADATA_BYTES:
            raise IsolatedRawError("byte_limit", "Decoder metadata exceeds its bound")
    except Exception as exc:
        header = json.dumps({"ok": False, "code": str(getattr(exc, "code", "worker_failed"))[:64],
                             "error": str(exc)[:2000]}).encode()
    sys.stdout.buffer.write(header + b"\n")
    sys.stdout.buffer.flush()
    return 0


def _linear_worker_main(argv: list[str]) -> int:
    output = argv[1] if len(argv) > 1 else ""
    try:
        path, output, max_pixels, address_space, max_source = argv
        _set_worker_limits(int(address_space), int(max_source), MAX_LINEAR_BYTES)
        metadata = _native_linear(path, output, max_pixels=int(max_pixels), max_source_bytes=int(max_source),
                                  address_space_bytes=int(address_space))
        header = json.dumps({"ok": True, **metadata}, separators=(",", ":")).encode()
        if len(header) > MAX_METADATA_BYTES:
            raise IsolatedRawError("byte_limit", "Decoder metadata exceeds its bound")
    except Exception as exc:
        if output:
            Path(output).unlink(missing_ok=True)
        header = json.dumps({"ok": False, "code": str(getattr(exc, "code", "worker_failed"))[:64],
                             "error": str(exc)[:2000]}).encode()
    sys.stdout.buffer.write(header + b"\n")
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        raise SystemExit(_worker_main(sys.argv[2:]))
    if sys.argv[1:2] == ["--worker-linear"]:
        raise SystemExit(_linear_worker_main(sys.argv[2:]))
    raise SystemExit("Use isolated_raw.decode_arw_isolated or render_arw_linear_isolated; this file is not a CLI")
