"""Lossless container export of our rendered linear-sRGB TIFF, not sensor RAW.

This product includes DNG technology under license by Adobe.
DNG 1.2 ColorimetricReference=1 marks output-referred data. Source precision and
rendering history remain explicit. Only our hash-matched development files enter
this writer; a supervised independent LibRaw decode must preserve every sample.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import stat
import struct
import subprocess
import sys
import time
import threading
import uuid

MAX_PIXELS = 40_000_000
MAX_TIFF_BYTES = MAX_PIXELS * 6 + 65536
MAX_VERIFY_SECONDS = 30
MAX_VERIFY_RSS = 1024 * 1024 * 1024
_EXPORT_LOCK = threading.Lock()
# W3C CSS Color 4, sRGB primaries / D65, XYZ -> linear sRGB. These characterize
# a defined RGB encoding, not the source camera's sensor or measured calibration.
XYZ_D65_TO_SRGB = (
    (12831 / 3959, -329 / 214, -1974 / 3959),
    (-851781 / 878810, 1648619 / 878810, 36519 / 878810),
    (705 / 12673, -2585 / 12673, 705 / 667),
)
DESCRIPTION = 'Camera Autopilot JPEG-derived output-referred linear RGB; not sensor RAW; 16-bit storage does not restore JPEG precision.'
RAW_DESCRIPTION = 'Camera Autopilot RAW-derived output-referred linear RGB; camera colour matrix and as-shot white balance; not mosaiced sensor RAW; 16-bit developed samples.'


class DNGError(ValueError):
    def __init__(self, message, *, reason='invalid_input', job_id=None):
        super().__init__(message)
        self.reason = reason
        self.job_id = job_id
        self.retry_capture = False


def _sha_stream(stream, count=None):
    digest = hashlib.sha256()
    remaining = count
    total = 0
    while remaining is None or remaining > 0:
        chunk = stream.read(1024 * 1024 if remaining is None else min(remaining, 1024 * 1024))
        if not chunk:
            if remaining not in (None, 0):
                raise DNGError('Truncated pixel data.')
            break
        total += len(chunk)
        if total > MAX_TIFF_BYTES:
            raise DNGError('Input grew beyond the file byte budget.')
        digest.update(chunk)
        if remaining is not None:
            remaining -= len(chunk)
    return digest.hexdigest()


def _open_regular(path, limit):
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    stream = os.fdopen(fd, 'rb')
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
        stream.close()
        raise DNGError('Input must be a bounded regular file.')
    return stream


def _read_exact(stream, size):
    data = stream.read(size)
    if len(data) != size:
        raise DNGError('Truncated TIFF header.')
    return data


def inspect_development_tiff(stream):
    """Parse only the exact uncompressed strip layout our developer produces."""
    stream.seek(0)
    if _read_exact(stream, 8) != b'II*\x00\x08\x00\x00\x00':
        raise DNGError('Expected our little-endian baseline TIFF.')
    count, = struct.unpack('<H', _read_exact(stream, 2))
    if count != 12:
        raise DNGError('Unsupported TIFF directory; only our developer output is accepted.')
    entries = {}
    for _ in range(count):
        tag, kind, length, offset = struct.unpack('<HHII', _read_exact(stream, 12))
        if tag in entries:
            raise DNGError('Duplicate TIFF tag.')
        entries[tag] = (kind, length, offset)
    if _read_exact(stream, 4) != b'\x00' * 4:
        raise DNGError('Extra TIFF directories are not supported.')
    allowed = {256, 257, 258, 259, 262, 270, 273, 274, 277, 278, 279, 339}
    if set(entries) != allowed:
        raise DNGError('TIFF tags do not match the development contract.')
    def scalar(tag, kind):
        actual, length, value = entries[tag]
        if actual != kind or length != 1:
            raise DNGError(f'Invalid TIFF tag {tag}.')
        return value
    width, height = scalar(256, 4), scalar(257, 4)
    if min(width, height) < 1 or width * height > MAX_PIXELS:
        raise DNGError('TIFF dimensions exceed the export budget.')
    for tag, kind, value in [(259, 3, 1), (262, 3, 2), (274, 3, 1), (277, 3, 3), (278, 4, height), (279, 4, width * height * 6)]:
        if scalar(tag, kind) != value:
            raise DNGError(f'Unsupported TIFF value for tag {tag}.')
    minimum_extra = 8 + 2 + 12 * count + 4
    expected_description = b'Camera Autopilot linear sRGB primaries; 16-bit unsigned RGB; not DNG\x00'
    for tag, expected, kind in [(258, struct.pack('<HHH', 16, 16, 16), 3), (339, struct.pack('<HHH', 1, 1, 1), 3), (270, expected_description, 2)]:
        actual_kind, length, offset = entries[tag]
        if actual_kind != kind or length != (len(expected) // 2 if kind == 3 else len(expected)) or not minimum_extra <= offset <= 65536 - len(expected):
            raise DNGError(f'Invalid TIFF payload for tag {tag}.')
        stream.seek(offset)
        if _read_exact(stream, len(expected)) != expected:
            raise DNGError('TIFF color/precision declaration does not match our linear RGB format.')
    pixels = scalar(273, 4)
    if not entries[270][2] + len(expected_description) <= pixels <= 65536:
        raise DNGError('Pixel offset overlaps TIFF metadata or exceeds the header budget.')
    if os.fstat(stream.fileno()).st_size != pixels + width * height * 6:
        raise DNGError('TIFF has trailing or missing data.')
    return {'width': width, 'height': height, 'pixel_offset': pixels, 'pixel_bytes': width * height * 6}


def validate_input(tiff_path, development_receipt_path):
    with _open_regular(development_receipt_path, 65536) as stream:
        encoded = stream.read(65537)
        if len(encoded) > 65536:
            raise DNGError('Development receipt exceeds byte budget.')
        provenance = json.loads(encoded)
        if not isinstance(provenance, dict):
            raise DNGError('Development receipt must be an object.')
    if provenance.get('status') != 'completed' or provenance.get('algorithm') != 'linear-development-v1' or provenance.get('source_kind') not in {'jpeg', 'raw'} or provenance.get('neural') is not False:
        raise DNGError('Only completed non-neural JPEG or RAW development receipts are accepted.')
    recorded = provenance.get('outputs', {}).get('linear.tiff', {})
    with _open_regular(tiff_path, MAX_TIFF_BYTES) as stream:
        metadata = inspect_development_tiff(stream)
        stream.seek(0)
        source_hash = _sha_stream(stream)
        if source_hash != recorded.get('sha256') or os.fstat(stream.fileno()).st_size != recorded.get('bytes'):
            raise DNGError('TIFF does not match its completed development receipt.', reason='hash_mismatch')
        if (metadata['width'], metadata['height']) != (provenance.get('width'), provenance.get('height')):
            raise DNGError('Development dimensions do not match TIFF.')
        stream.seek(metadata['pixel_offset'])
        pixels_hash = _sha_stream(stream, metadata['pixel_bytes'])
    source_file_hash = provenance.get('source_sha256', '')
    if len(source_file_hash) != 64 or any(char not in '0123456789abcdef' for char in source_file_hash):
        raise DNGError('Source provenance hash is missing.')
    identity = provenance.get('source_identity') if isinstance(provenance.get('source_identity'), dict) else {}
    source_kind = provenance.get('source_kind')
    return dict(metadata, tiff_sha256=source_hash, pixel_sha256=pixels_hash,
                source_jpeg_sha256=source_file_hash if source_kind == 'jpeg' else '',
                source_sha256=source_file_hash, source_kind=source_kind,
                source_make=identity.get('make') or '', source_model=identity.get('model') or '',
                development_job_id=provenance.get('job_id'), development_options=provenance.get('options', {}))


def _rational(values, signed=False):
    return b''.join(struct.pack('<ii' if signed else '<II', round(value * 1_000_000), 1_000_000) for value in values)


def _ascii_tag(value, fallback):
    text = ''.join(char for char in str(value or '') if 32 <= ord(char) < 127).strip()[:80]
    return text or fallback


def _header(metadata):
    width, height = metadata['width'], metadata['height']
    def shorts(*values): return struct.pack('<' + 'H' * len(values), *values)
    def longs(*values): return struct.pack('<' + 'I' * len(values), *values)
    def ascii_text(value): return value.encode('ascii') + b'\x00'
    raw = metadata.get('source_kind') == 'raw'
    origin = RAW_DESCRIPTION if raw else DESCRIPTION
    source_hash = metadata.get('source_sha256') or metadata.get('source_jpeg_sha256') or ''
    label = 'Source SHA256=' if raw else 'Source JPEG SHA256='
    description = origin + ' ' + label + source_hash + '; TIFF SHA256=' + metadata['tiff_sha256']
    make = _ascii_tag(metadata.get('source_make'), 'Camera Autopilot') if raw else 'Camera Autopilot'
    model = _ascii_tag(metadata.get('source_model'), 'Rendered linear sRGB derivative') if raw else 'Rendered linear sRGB derivative'
    unique = _ascii_tag(metadata.get('source_model'), 'Camera Autopilot RAW-derived linear sRGB') if raw else 'Camera Autopilot Rendered linear sRGB derivative'
    profile = 'Output-referred linear sRGB; RAW-derived; camera colour' if raw else 'Output-referred linear sRGB; JPEG-derived'
    tags = {
        254: (4, longs(0)), 256: (4, longs(width)), 257: (4, longs(height)),
        258: (3, shorts(16, 16, 16)), 259: (3, shorts(1)), 262: (3, shorts(34892)),
        270: (2, ascii_text(description)), 271: (2, ascii_text(make)),
        272: (2, ascii_text(model)),
        273: (4, longs(0)), 274: (3, shorts(1)), 277: (3, shorts(3)),
        278: (4, longs(height)), 279: (4, longs(metadata['pixel_bytes'])),
        284: (3, shorts(1)), 305: (2, ascii_text('Camera Autopilot output-referred DNG writer v1')),
        339: (3, shorts(1, 1, 1)),
        50706: (1, bytes([1, 2, 0, 0])), 50707: (1, bytes([1, 2, 0, 0])),
        50708: (2, ascii_text(unique)),
        50713: (3, shorts(1, 1)), 50714: (5, _rational([0, 0, 0])),
        50717: (4, longs(65535, 65535, 65535)), 50718: (5, _rational([1, 1])),
        50719: (4, longs(0, 0)), 50720: (4, longs(width, height)),
        50721: (10, _rational([v for row in XYZ_D65_TO_SRGB for v in row], signed=True)),
        50728: (5, _rational([1, 1, 1])), 50730: (10, _rational([0], signed=True)),
        50778: (3, shorts(21)), 50879: (3, shorts(1)),
        50936: (2, ascii_text(profile)),
        50940: (11, struct.pack('<ffff', 0, 0, 1, 1)),
    }
    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 10: 8, 11: 4}
    start = 8 + 2 + len(tags) * 12 + 4
    extras, entries = bytearray(), []
    for tag, (kind, payload) in sorted(tags.items()):
        if len(payload) <= 4:
            slot = payload.ljust(4, b'\x00')
        else:
            if (start + len(extras)) % 2:
                extras.append(0)
            slot = struct.pack('<I', start + len(extras))
            extras.extend(payload)
        entries.append((tag, kind, len(payload) // sizes[kind], slot))
    if (start + len(extras)) % 2:
        extras.append(0)
    pixels_offset = start + len(extras)
    header = bytearray(b'II*\x00' + struct.pack('<IH', 8, len(tags)))
    for tag, kind, count, slot in entries:
        header.extend(struct.pack('<HHI', tag, kind, count))
        header.extend(struct.pack('<I', pixels_offset) if tag == 273 else slot)
    header.extend(b'\x00' * 4)
    header.extend(extras)
    return bytes(header)


def _write_json(path, data):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as stream:
        json.dump(data, stream, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())


def _rss(pid):
    if sys.platform.startswith('linux'):
        try:
            for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) * 1024
        except FileNotFoundError:
            return 0
    result = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(pid)], capture_output=True, text=True, timeout=2)
    return int(result.stdout.strip() or 0) * 1024


def _export_linear_dng(tiff_path, development_receipt_path, output_root, *, verification_python=None):
    """Stream an exact RGB copy, publish only after bounded independent decode.

    verification_python is a trusted local runtime path, never a client command.
    The default current interpreter must have rawpy installed; absence fails closed.
    """
    metadata = validate_input(tiff_path, development_receipt_path)
    root = Path(output_root)
    if root.is_symlink():
        raise DNGError('Output root cannot be a symlink.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    job_id = uuid.uuid4().hex
    job = root / job_id
    job.mkdir(mode=0o700)
    candidate = job / 'candidate.dng'
    origin = 'RAW-derived' if metadata.get('source_kind') == 'raw' else 'JPEG-derived'
    receipt = dict(metadata, job_id=job_id, status='running', sensor_raw=False, neural=False,
                   encoding='DNG 1.2 LinearRaw RGB uint16; output-referred; ' + origin,
                   source_precision_restored=False, source_preserved=True)
    process = None
    started = time.monotonic()
    peak = 0
    try:
        with _open_regular(tiff_path, MAX_TIFF_BYTES) as original, os.fdopen(os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as output:
            current = inspect_development_tiff(original)
            if current != {key: metadata[key] for key in current}:
                raise DNGError('Source layout changed.', reason='source_changed')
            original.seek(0)
            if _sha_stream(original) != metadata['tiff_sha256']:
                raise DNGError('Source changed before export.', reason='source_changed')
            output.write(_header(metadata))
            original.seek(metadata['pixel_offset'])
            remaining = metadata['pixel_bytes']
            digest = hashlib.sha256()
            while remaining:
                chunk = original.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise DNGError('Source changed during export.', reason='source_changed')
                output.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            if digest.hexdigest() != metadata['pixel_sha256']:
                raise DNGError('Source pixels changed during export.', reason='source_changed')
            output.flush()
            os.fsync(output.fileno())
        _write_json(job / 'verify-request.json', metadata)
        with os.fdopen(os.open(job / 'verify.log', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as log:
            process = subprocess.Popen([str(verification_python or sys.executable), str(Path(__file__).resolve()), '--verify', str(job.resolve())], stdout=log, stderr=log, start_new_session=True,
                                       env=dict(os.environ, OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1'))
            while process.poll() is None:
                peak = max(peak, _rss(process.pid))
                if time.monotonic() - started > MAX_VERIFY_SECONDS or peak > MAX_VERIFY_RSS:
                    raise DNGError('Independent decoder exceeded bounds.', reason='verification_limit')
                time.sleep(.1)
        proof_path = job / 'verification.json'
        proof = json.loads(proof_path.read_text()) if proof_path.is_file() else {}
        if process.returncode or proof.get('status') != 'verified' or proof.get('pixel_sha256') != metadata['pixel_sha256']:
            raise DNGError(proof.get('error', 'Independent LibRaw decoder is unavailable or rejected the DNG.'), reason='decoder_verification_failed')
        candidate.rename(job / 'output.dng')
        with (job / 'output.dng').open('rb') as final:
            output_sha = _sha_stream(final)
        receipt.update(status='completed', output={'filename': 'output.dng', 'sha256': output_sha, 'bytes': (job / 'output.dng').stat().st_size}, verification=proof)
    except Exception as exc:
        if process and process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
        candidate.unlink(missing_ok=True)
        (job / 'output.dng').unlink(missing_ok=True)
        receipt.update(status='failed', reason=getattr(exc, 'reason', 'export_failed'), error=str(exc))
        raise DNGError(str(exc), reason=receipt['reason'], job_id=job_id) from exc
    finally:
        receipt.update(elapsed_seconds=time.monotonic() - started, observed_verify_peak_rss_bytes=peak)
        _write_json(job / 'receipt.json', receipt)
    return receipt


def export_linear_dng(tiff_path, development_receipt_path, output_root, *, verification_python=None):
    if not _EXPORT_LOCK.acquire(blocking=False):
        raise DNGError('Another DNG export is processing.', reason='busy')
    try:
        return _export_linear_dng(tiff_path, development_receipt_path, output_root, verification_python=verification_python)
    finally:
        _EXPORT_LOCK.release()


def _verify(job):
    import resource
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_CPU, (30, 31))
    resource.setrlimit(resource.RLIMIT_FSIZE, (1024 * 1024, 1024 * 1024))
    signal.alarm(31)
    if sys.platform.startswith('linux'):
        resource.setrlimit(resource.RLIMIT_AS, (MAX_VERIFY_RSS, MAX_VERIFY_RSS))
    try:
        import rawpy
        import numpy as np
        request = json.loads((job / 'verify-request.json').read_text())
        with rawpy.imread(str(job / 'candidate.dng')) as raw:
            data = raw.raw_image
            if data.ndim != 3 or data.shape[:2] != (request['height'], request['width']) or data.shape[2] not in (3, 4) or data.dtype != np.uint16 or raw.num_colors != 3:
                raise DNGError('Independent decoder shape or precision mismatch.')
            digest = hashlib.sha256()
            for row in data:
                if row.shape[1] == 4 and np.any(row[:, 3]):
                    raise DNGError('Independent decoder has unexpected fourth-channel data.')
                digest.update(row[:, :3].astype('<u2', copy=False).tobytes())
            if digest.hexdigest() != request['pixel_sha256']:
                raise DNGError('Independent decoder did not preserve RGB samples.')
            proof = {'status': 'verified', 'decoder': 'LibRaw via rawpy', 'libraw_version': list(rawpy.libraw_version), 'rawpy_version': rawpy.__version__,
                     'shape': [request['height'], request['width'], 3], 'decoder_buffer_shape': list(data.shape), 'pixel_sha256': digest.hexdigest(), 'sample_equality': 'all RGB uint16 samples exact',
                     'adobe_dng_validate': 'unavailable; no Adobe SDK conformance or Lightroom rendering claim'}
        _write_json(job / 'verification.json', proof)
        return 0
    except Exception as exc:
        _write_json(job / 'verification.json', {'status': 'failed', 'error': str(exc)})
        return 1


if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] != '--verify':
        raise SystemExit('Use tools/export_linear_dng.py.')
    raise SystemExit(_verify(Path(sys.argv[2])))
