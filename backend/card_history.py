"""Bounded read-only camera card browsing and explicit selected-file import.

No wake, capture, settings writes, card deletion, or formatting is available here.
The app must hold its camera-operation exclusion while invoking this standalone API.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import uuid

MAX_FOLDERS = 128
MAX_FILES = 2000
MAX_DEPTH = 8
MAX_IMAGE_BYTES = 128 * 1024 * 1024
MAX_SECONDS = 30
MAX_MEMORY_BYTES = 512 * 1024 * 1024
SUPPORTED_MODELS = {'Sony ILCE-7M2 (Control)', 'Sony Alpha-A7 III (PC Control)'}
PHOTO_SUFFIXES = {'.jpg', '.jpeg', '.arw', '.dng', '.tif', '.tiff', '.png'}


class CardHistoryError(RuntimeError):
    def __init__(self, message, reason='card_read_failed'):
        super().__init__(message)
        self.reason = reason
        self.retry_capture = False


def _identity(model, port):
    if model not in SUPPORTED_MODELS or not isinstance(port, str) or not port.startswith('usb:') or len(port) > 100 or any(c in port for c in '\r\n\x00'):
        raise CardHistoryError('Select the exact supported model and USB port.', 'invalid_identity')


def _prove_card_identity(model, port, serial):
    """Refuse a missing or wrong USB serial before any card listing, download, or job directory."""
    _identity(model, port)
    repo = str(Path(__file__).resolve().parents[1])
    if repo not in sys.path:
        sys.path.insert(0, repo)
    from sonyremote.camera import verify_serial_at_port
    verify_serial_at_port(model, port, serial)


def _name(name):
    if not isinstance(name, str) or not name or len(name) > 255 or name in {'.', '..'} or '/' in name or '\\' in name or any(ord(c) < 32 for c in name):
        raise CardHistoryError('Invalid card name.', 'invalid_path')
    return name


def _folder(folder):
    if not isinstance(folder, str) or not folder.startswith('/') or '\\' in folder or len(folder) > 2048:
        raise CardHistoryError('Card folder must be an absolute card path.', 'invalid_path')
    if folder == '/':
        return folder
    parts = folder[1:].split('/')
    if len(parts) > MAX_DEPTH:
        raise CardHistoryError('Card folder exceeds depth budget.', 'limit')
    for part in parts:
        _name(part)
    return '/' + '/'.join(parts)


def list_from_adapter(adapter):
    """Pure adapter operation; metadata is card-reported, not content validation."""
    queue = [('/', 0)]
    seen = set()
    files = []
    started = time.monotonic()
    while queue:
        if time.monotonic() - started > MAX_SECONDS:
            raise CardHistoryError('Card listing exceeded its deadline.', 'timeout')
        folder, depth = queue.pop(0)
        if folder in seen:
            raise CardHistoryError('Repeated card folder.', 'invalid_path')
        seen.add(folder)
        if len(seen) > MAX_FOLDERS:
            raise CardHistoryError('Card folder budget exceeded.', 'limit')
        for name in adapter.folders(folder):
            _name(name)
            if depth + 1 > MAX_DEPTH or len(queue) + len(seen) >= MAX_FOLDERS:
                raise CardHistoryError('Card folder/depth budget exceeded.', 'limit')
            queue.append((folder.rstrip('/') + '/' + name, depth + 1))
        for name in adapter.files(folder):
            _name(name)
            if len(files) >= MAX_FILES:
                raise CardHistoryError('Card file budget exceeded.', 'limit')
            info = adapter.info(folder, name)
            files.append({'folder': folder, 'name': name, 'bytes': info.get('bytes'),
                          'mime_type': info.get('mime_type'),
                          'downloadable': Path(name).suffix.lower() in PHOTO_SUFFIXES and isinstance(info.get('bytes'), int) and not isinstance(info.get('bytes'), bool) and 0 < info['bytes'] <= MAX_IMAGE_BYTES,
                          'metadata_evidence': 'camera-reported; file content not downloaded'})
    return {'status': 'completed', 'folders': sorted(seen), 'files': files,
            'file_count': len(files), 'read_only': True, 'retry_capture': False}


def download_from_adapter(adapter, folder, name, destination):
    folder, name = _folder(folder), _name(name)
    if Path(name).suffix.lower() not in PHOTO_SUFFIXES:
        raise CardHistoryError('Only explicitly selected photo files can be imported.', 'invalid_path')
    # Membership and both size reads bind this request to the selected card entry.
    found = False
    for index, candidate in enumerate(adapter.files(folder)):
        if index >= MAX_FILES:
            raise CardHistoryError('Card file budget exceeded.', 'limit')
        _name(candidate)
        found |= candidate == name
    if not found:
        raise CardHistoryError('Selected file is absent from the card folder.', 'not_found')
    before = adapter.info(folder, name)
    size = before.get('bytes')
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= MAX_IMAGE_BYTES:
        raise CardHistoryError('Card file has missing or excessive size metadata.', 'limit')
    adapter.download(folder, name, destination)
    after = adapter.info(folder, name)
    if before != after or destination.is_symlink() or not destination.is_file() or destination.stat().st_size != size:
        raise CardHistoryError('Card file changed or downloaded size mismatched.', 'source_changed')
    with destination.open('rb') as handle:
        digest = hashlib.file_digest(handle, 'sha256').hexdigest()
    return {'status': 'completed', 'folder': folder, 'name': name, 'bytes': size, 'sha256': digest,
            'source_metadata': before, 'read_only': True, 'card_deleted': False, 'retry_capture': False,
            'content_evidence': 'selected file bytes downloaded and hashed; image decode not implied'}


class GPhotoCardAdapter:
    def __init__(self, camera, gp):
        self.camera, self.gp = camera, gp

    def folders(self, folder):
        return (name for name, _ in self.camera.folder_list_folders(folder))

    def files(self, folder):
        return (name for name, _ in self.camera.folder_list_files(folder))

    def info(self, folder, name):
        item = self.camera.file_get_info(folder, name).file
        return {'bytes': int(item.size) if item.fields & self.gp.GP_FILE_INFO_SIZE else None,
                'mime_type': item.type if item.fields & self.gp.GP_FILE_INFO_TYPE else None,
                'mtime': int(item.mtime) if item.fields & self.gp.GP_FILE_INFO_MTIME else None}

    def download(self, folder, name, destination):
        # FD-backed transfer keeps the library from intentionally accumulating a whole
        # file in Python memory; the child also has file-size/RSS/deadline enforcement.
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            camera_file = self.gp.CameraFile(fd)
            self.camera.file_get(folder, name, self.gp.GP_FILE_TYPE_NORMAL, camera_file)
            # CameraFile owns the fd; flush the same descriptor before releasing it.
            os.fsync(fd)
            del camera_file
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise


def _worker(request_path):
    import resource
    os.umask(0o077)
    request = json.loads(Path(request_path).read_text())
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_IMAGE_BYTES + 1, MAX_IMAGE_BYTES + 1))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_SECONDS, MAX_SECONDS + 1))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    signal.alarm(MAX_SECONDS + 1)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sonyremote import Camera, CameraIdentityError
    import gphoto2 as gp
    _identity(request['model'], request['port'])
    serial = request.get('serial')
    if not serial:
        raise CameraIdentityError(
            'Select the camera again: its USB serial is required before a card read.',
            reason='serial_required', model=request['model'], port=request['port'])
    with Camera(model=request['model'], port=request['port'], serial=serial) as selected:
        adapter = GPhotoCardAdapter(selected._camera, gp)
        if request['operation'] == 'list':
            result = list_from_adapter(adapter)
        elif request['operation'] == 'download':
            result = download_from_adapter(adapter, request['folder'], request['name'], Path(request['destination']))
        else:
            raise CardHistoryError('Unknown card read operation.')
        result.update({'model': selected.info().model, 'port': selected.info().port})
    Path(request_path).with_name('result.json').write_text(json.dumps(result))


def _run(request, cancel_event=None):
    _identity(request.get('model'), request.get('port'))
    if cancel_event and cancel_event.is_set():
        raise CardHistoryError('Card read canceled before connecting.', 'canceled')
    with tempfile.TemporaryDirectory(prefix='camera-card-read-') as temporary:
        root = Path(temporary)
        request_path = root / 'request.json'
        request_path.write_text(json.dumps(request))
        request_path.chmod(0o600)
        process = None
        try:
            with (root / 'worker.log').open('wb') as log:
                process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--worker', str(request_path)], stdout=log, stderr=log, start_new_session=True)
                started = time.monotonic()
                while process.poll() is None:
                    if cancel_event and cancel_event.is_set():
                        raise CardHistoryError('Card read canceled.', 'canceled')
                    if time.monotonic() - started > MAX_SECONDS:
                        raise CardHistoryError('Card read exceeded its deadline.', 'timeout')
                    from isolated_development import _rss_bytes
                    if _rss_bytes(process.pid) > MAX_MEMORY_BYTES:
                        raise CardHistoryError('Card read exceeded its memory budget.', 'memory_limit')
                    time.sleep(.05)
            if cancel_event and cancel_event.is_set():
                raise CardHistoryError('Card read canceled.', 'canceled')
            if process.returncode != 0:
                failure = root / 'result.json'
                if failure.is_file() and failure.stat().st_size <= 4096:
                    detail = json.loads(failure.read_text())
                    raise CardHistoryError(detail.get('message', 'Read-only card operation failed.'), detail.get('reason', 'card_read_failed'))
                raise CardHistoryError('Camera rejected the read-only card operation; no capture or write was attempted.')
            result_path = root / 'result.json'
            if result_path.stat().st_size > 4 * 1024 * 1024:
                raise CardHistoryError('Card result exceeded its byte budget.', 'limit')
            return json.loads(result_path.read_text())
        finally:
            if process and process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait(timeout=5)


def list_card(model, port, *, serial=None, cancel_event=None):
    _prove_card_identity(model, port, serial)
    return _run({'operation': 'list', 'model': model, 'port': port, 'serial': serial}, cancel_event)


def download_card_file(model, port, folder, name, destination_root, *, serial=None, cancel_event=None):
    _prove_card_identity(model, port, serial)
    folder, name = _folder(folder), _name(name)
    root = Path(destination_root)
    if root.is_symlink():
        raise CardHistoryError('Import root cannot be a symbolic link.', 'invalid_path')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # An explicit destination may contain operator-owned files; only our new job is chmodded.
    job = root / ('card_' + uuid.uuid4().hex)
    job.mkdir(mode=0o700)
    destination = job / name
    try:
        result = _run({'operation': 'download', 'model': model, 'port': port, 'serial': serial,
                       'folder': folder, 'name': name, 'destination': str(destination.resolve())}, cancel_event)
        if destination.is_symlink() or not destination.is_file():
            raise CardHistoryError('Imported file missing.', 'invalid_output')
        with destination.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if destination.stat().st_size != result['bytes'] or digest != result['sha256']:
            raise CardHistoryError('Imported output failed independent hash verification.', 'invalid_output')
        result['job_id'] = job.name
        result['image'] = name
        receipt = job / 'receipt.json'
        receipt.write_text(json.dumps(result, indent=2))
        receipt.chmod(0o600)
        return result
    except Exception:
        destination.unlink(missing_ok=True)
        if not any(job.iterdir()):
            job.rmdir()
        raise


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--worker':
        try:
            _worker(sys.argv[2])
        except Exception as error:
            Path(sys.argv[2]).with_name('result.json').write_text(json.dumps({'status': 'failed', 'message': str(error)[:2048], 'reason': getattr(error, 'reason', 'card_read_failed'), 'retry_capture': False}))
            raise SystemExit(1)
    else:
        raise SystemExit('Use tools/card_history.py for explicit read-only commands.')
