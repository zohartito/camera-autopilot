"""Private, bounded, camera-free development jobs; failure never requests a capture."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import threading
import time
import uuid

MAX_SOURCE_BYTES = 128 * 1024 * 1024
MAX_PIXELS = 40_000_000
MAX_MEMORY_BYTES = 1536 * 1024 * 1024
MAX_SECONDS = 90
_LOCK = threading.Lock()


class DevelopmentError(RuntimeError):
    def __init__(self, message, *, reason='invalid_source', job_id=None):
        super().__init__(message)
        self.reason = reason
        self.job_id = job_id
        self.retry_capture = False


def validate_options(options=None):
    supplied = dict(options or {})
    defaults = {'exposure_ev': 0.0, 'wb_red': 1.0, 'wb_blue': 1.0,
                'highlight_compression': 0.0, 'tone_strength': 0.0}
    if set(supplied) - set(defaults):
        raise ValueError('Unknown development option.')
    limits = {'exposure_ev': (-3, 3), 'wb_red': (.5, 2), 'wb_blue': (.5, 2),
              'highlight_compression': (0, 1), 'tone_strength': (0, 1)}
    defaults.update(supplied)
    for name, value in defaults.items():
        low, high = limits[name]
        if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f'{name} must be finite and between {low} and {high}.')
        defaults[name] = float(value)
    return defaults


def _rss_bytes(pid):
    if sys.platform.startswith('linux'):
        try:
            for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) * 1024
        except FileNotFoundError:
            return 0
    result = subprocess.run(['/bin/ps', '-o', 'rss=', '-p', str(pid)], capture_output=True, text=True, timeout=2)
    return int(result.stdout.strip() or '0') * 1024


def _private_write(path, payload):
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'w') as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())


def _copy_source(source, destination):
    fd = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as original:
        before = os.fstat(original.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_SOURCE_BYTES:
            raise DevelopmentError('Source must be a regular file no larger than 128 MiB.')
        digest = hashlib.sha256()
        with os.fdopen(os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), 'wb') as copied:
            count = 0
            while chunk := original.read(min(1024 * 1024, MAX_SOURCE_BYTES + 1 - count)):
                count += len(chunk)
                if count > MAX_SOURCE_BYTES:
                    raise DevelopmentError('Source grew beyond byte budget.')
                digest.update(chunk)
                copied.write(chunk)
            copied.flush()
            os.fsync(copied.fileno())
        after = os.fstat(original.fileno())
        identity = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if identity(before) != identity(after) or count != before.st_size:
            raise DevelopmentError('Source changed while preparing development.')
        return digest.hexdigest(), identity(before)


def _ascii_identity(value):
    return ''.join(char for char in str(value or '') if 32 <= ord(char) < 127).strip()[:80]


def run_development(source, output_root, options=None, *, cancel_event=None):
    """Develop an existing local file synchronously, suitable for a background job.

    Caller must restrict sources to its owned gallery/session. No camera or network
    operations exist here. A canceled/failed job retains only a private error receipt.
    """
    options = validate_options(options)
    source = Path(source)
    if source.is_symlink() or source.suffix.lower() not in {'.jpg', '.jpeg', '.arw', '.dng', '.nef', '.cr2', '.cr3'}:
        raise DevelopmentError('Select an owned JPEG or supported RAW file.')
    if source.suffix.lower() not in {'.jpg', '.jpeg', '.arw'}:
        raise DevelopmentError('Only JPEG and Sony ARW development are supported.', reason='raw_unavailable')
    if source.suffix.lower() not in {'.jpg', '.jpeg'} and not sys.platform.startswith('linux'):
        raise DevelopmentError('RAW decoding is disabled here: this platform has no verified hard address-space limit. JPEG development is available.', reason='raw_unavailable')
    root = Path(output_root)
    if root.is_symlink():
        raise DevelopmentError('Output root cannot be a symbolic link.')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    job_id = uuid.uuid4().hex
    job = root / job_id
    job.mkdir(mode=0o700)
    started = time.monotonic()
    process = None
    peak = 0
    reason = 'development_failed'
    try:
        with _LOCK:
            if cancel_event and cancel_event.is_set():
                raise DevelopmentError('Development canceled.', reason='canceled')
            staged = job / ('source' + source.suffix.lower())
            digest, identity = _copy_source(source, staged)
            request = {'source': str(staged.resolve()), 'options': options, 'max_pixels': MAX_PIXELS,
                       'memory_bytes': MAX_MEMORY_BYTES, 'seconds': MAX_SECONDS}
            if source.suffix.lower() == '.arw':
                import isolated_raw
                try:
                    header = isolated_raw.inspect_arw_header(staged)
                    camera_linear = job / 'camera_linear.tiff'
                    rendered = isolated_raw.render_arw_linear_isolated(staged, camera_linear)
                except isolated_raw.IsolatedRawError as exc:
                    raise DevelopmentError(str(exc), reason=exc.code)
                request.update(camera_linear=str(camera_linear.resolve()),
                               linear_width=rendered.width, linear_height=rendered.height,
                               colour={'source': rendered.colour_source,
                                       'whitebalance': list(rendered.camera_whitebalance),
                                       'rgb_xyz_matrix': [list(row) for row in rendered.rgb_xyz_matrix],
                                       'decoder': rendered.decoder,
                                       'pixel_sha256': rendered.pixel_sha256},
                               source_identity={'make': _ascii_identity(header.get('make')),
                                                'model': _ascii_identity(header.get('model'))})
            _private_write(job / 'request.json', request)
            with (job / 'worker.log').open('xb') as log:
                os.chmod(job / 'worker.log', 0o600)
                env = dict(os.environ, OPENBLAS_NUM_THREADS='1', OMP_NUM_THREADS='1', MKL_NUM_THREADS='1')
                process = subprocess.Popen([sys.executable, str(Path(__file__).with_name('development_worker.py')), str((job / 'request.json').resolve())], stdout=log, stderr=log, env=env, start_new_session=True)
                worker_started = time.monotonic()
                while process.poll() is None:
                    if cancel_event and cancel_event.is_set():
                        raise DevelopmentError('Development canceled.', reason='canceled')
                    if time.monotonic() - worker_started > MAX_SECONDS:
                        raise DevelopmentError('Development exceeded its time limit.', reason='timeout')
                    peak = max(peak, _rss_bytes(process.pid))
                    if peak > MAX_MEMORY_BYTES:
                        raise DevelopmentError('Development exceeded its memory budget.', reason='memory_limit')
                    time.sleep(.05)
            if cancel_event and cancel_event.is_set():
                raise DevelopmentError('Development canceled.', reason='canceled')
            if process.returncode != 0:
                raise DevelopmentError('Decoder or renderer failed; original preserved.', reason='worker_failed')
            receipt = json.loads((job / 'render.json').read_text())
            current = source.stat(follow_symlinks=False)
            if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns, current.st_ctime_ns) != identity:
                raise DevelopmentError('Original changed during development; output withheld.', reason='source_changed')
            outputs = {}
            for filename in ['developed.jpg', 'linear.tiff', 'preview.jpg']:
                path = job / filename
                if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 256 * 1024 * 1024:
                    raise DevelopmentError('Renderer output is invalid.', reason='invalid_output')
                with path.open('rb') as handle:
                    output_hash = hashlib.file_digest(handle, 'sha256').hexdigest()
                outputs[filename] = {'bytes': path.stat().st_size, 'sha256': output_hash}
            receipt.update({'job_id': job_id, 'status': 'completed', 'source_image': source.name, 'source_sha256': digest,
                            'source_preserved': True, 'options': options, 'outputs': outputs,
                            'image': 'developed.jpg', 'preview': 'preview.jpg', 'linear_tiff': 'linear.tiff',
                            'elapsed_seconds': round(time.monotonic() - started, 3), 'observed_peak_rss_bytes': peak,
                            'memory_enforcement': 'hard RLIMIT_AS plus RSS watchdog' if sys.platform.startswith('linux') else 'RSS watchdog; bounded JPEG dimensions and chunked processing',
                            'retry_capture': False})
            _private_write(job / 'receipt.json', receipt)
            staged.unlink()
            (job / 'request.json').unlink()
            (job / 'render.json').unlink()
            camera_linear = job / 'camera_linear.tiff'
            if camera_linear.is_file() and not camera_linear.is_symlink():
                camera_linear.unlink()
            return receipt
    except Exception as exc:
        if process and process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
        reason = getattr(exc, 'reason', reason)
        for path in job.iterdir():
            if path.is_file() and path.name != 'worker.log':
                path.unlink()
        _private_write(job / 'receipt.json', {'job_id': job_id, 'status': 'canceled' if reason == 'canceled' else 'failed',
                       'reason': reason, 'original_deleted': False, 'retry_capture': False})
        if isinstance(exc, DevelopmentError):
            exc.job_id = job_id
            raise
        raise DevelopmentError('Development failed; original was not deleted.', reason=reason, job_id=job_id) from exc
