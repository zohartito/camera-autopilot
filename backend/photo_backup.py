"""Verified local gallery copies to an explicit operator-chosen destination.

Copy verification is not proof of independent physical media. No deletion or
remote upload occurs. The app should hold its gallery lifecycle exclusion.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
import uuid

MAX_FILES = 1000
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024 * 1024
MAX_SECONDS = 120
PHOTO_SUFFIXES = {'.jpg', '.jpeg', '.png', '.tif', '.tiff', '.arw', '.dng'}
_LOCK = threading.Lock()


class BackupError(RuntimeError):
    def __init__(self, message, reason='backup_failed'):
        super().__init__(message)
        self.reason = reason
        self.original_deleted = False


def _tick(started, cancel_event):
    if cancel_event and cancel_event.is_set():
        raise BackupError('Backup canceled; original files remain.', 'canceled')
    if time.monotonic() - started > MAX_SECONDS:
        raise BackupError('Backup exceeded its deadline; verified prior copies remain resumable.', 'timeout')


def _identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _hash(path, started, cancel_event):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_FILE_BYTES:
            raise BackupError('Backup copy has an invalid type or byte size.', 'invalid_copy')
        digest = hashlib.sha256()
        count = 0
        while chunk := handle.read(1024 * 1024):
            _tick(started, cancel_event)
            count += len(chunk)
            if count > MAX_FILE_BYTES:
                raise BackupError('Copy exceeds its byte budget.', 'limit')
            digest.update(chunk)
        if _identity(info) != _identity(os.fstat(handle.fileno())) or count != info.st_size:
            raise BackupError('File changed while hashing.', 'source_changed')
        return digest.hexdigest(), count


def _directory(path):
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        info = path.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_uid != os.geteuid():
            raise BackupError('Backup-owned directory is not safe.', 'invalid_destination')
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise BackupError('Existing backup-owned directory must be private (0700).', 'invalid_destination')


def backup_gallery(source_root, destination_root, images=None, *, cancel_event=None):
    """Copy selected root-level photos; None selects all root-level photo files.

    Content-addressed copies are reused only after complete independent SHA256
    verification. A failed/canceled run leaves verified previous copies for resume.
    Source metadata remains inside byte-identical photos; receipts are private.
    """
    source = Path(source_root)
    destination = Path(destination_root)
    if source.is_symlink() or destination.is_symlink() or not source.is_dir() or not destination.is_dir():
        raise BackupError('Choose existing regular source and destination directories.', 'invalid_destination')
    source, destination = source.resolve(), destination.resolve()
    if destination == source or source in destination.parents or destination in source.parents:
        raise BackupError('Backup destination must be outside the gallery and its ancestor folders.', 'invalid_destination')
    started = time.monotonic()
    with _LOCK:
        _tick(started, cancel_event)
        if images is None:
            names = []
            for path in source.iterdir():
                if path.suffix.lower() in PHOTO_SUFFIXES:
                    names.append(path.name)
                    if len(names) > MAX_FILES:
                        raise BackupError('Gallery exceeds the file-count budget.', 'limit')
            names.sort()
        else:
            if not isinstance(images, (list, tuple)):
                raise BackupError('Images must be an explicit filename list.', 'invalid_source')
            names = list(images)
        if not names or len(names) > MAX_FILES:
            raise BackupError('Select 1–1000 unique photo filenames.', 'limit')
        for name in names:
            if not isinstance(name, str) or Path(name).name != name or '\\' in name or any(ord(c) < 32 for c in name) or Path(name).suffix.lower() not in PHOTO_SUFFIXES:
                raise BackupError('Invalid selected photo filename.', 'invalid_source')
        if len(set(names)) != len(names):
            raise BackupError('Select unique photo filenames.', 'invalid_source')
        backup = destination / 'camera-autopilot-backup'
        _directory(backup)
        files, receipts = backup / 'files', backup / 'receipts'
        _directory(files)
        _directory(receipts)
        source_device, destination_device = source.stat().st_dev, files.stat().st_dev
        manifest = []
        total = 0
        for name in names:
            _tick(started, cancel_event)
            original = source / name
            temporary = files / ('.partial-' + uuid.uuid4().hex)
            try:
                fd = os.open(original, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(fd, 'rb') as handle:
                    initial = os.fstat(handle.fileno())
                    if not stat.S_ISREG(initial.st_mode) or not 0 < initial.st_size <= MAX_FILE_BYTES:
                        raise BackupError('Selected source is not a bounded regular file.', 'invalid_source')
                    total += initial.st_size
                    if total > MAX_TOTAL_BYTES:
                        raise BackupError('Backup exceeds total byte budget.', 'limit')
                    digest = hashlib.sha256()
                    count = 0
                    with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'wb') as copied:
                        while chunk := handle.read(1024 * 1024):
                            _tick(started, cancel_event)
                            count += len(chunk)
                            if count > initial.st_size:
                                raise BackupError('Source grew during backup.', 'source_changed')
                            digest.update(chunk)
                            copied.write(chunk)
                        copied.flush()
                        os.fsync(copied.fileno())
                    if count != initial.st_size or _identity(initial) != _identity(os.fstat(handle.fileno())) or _identity(initial) != _identity(original.stat(follow_symlinks=False)):
                        raise BackupError('Source changed during backup.', 'source_changed')
                sha = digest.hexdigest()
                copy_hash, copy_size = _hash(temporary, started, cancel_event)
                if (sha, count) != (copy_hash, copy_size):
                    raise BackupError('Backup copy hash does not match source.', 'verification_failed')
                target_name = sha + original.suffix.lower()
                target = files / target_name
                reused = False
                try:
                    # Atomic no-overwrite publication; collisions are verified, never replaced.
                    os.link(temporary, target, follow_symlinks=False)
                except FileExistsError:
                    existing_info = target.stat(follow_symlinks=False)
                    if (existing_info.st_dev, existing_info.st_ino) == (initial.st_dev, initial.st_ino):
                        raise BackupError('Existing backup aliases the source file; refusing to call it a second copy.', 'verification_failed')
                    existing_sha, existing_size = _hash(target, started, cancel_event)
                    if (existing_sha, existing_size) != (sha, count):
                        raise BackupError('Existing backup content was altered; refusing to overwrite.', 'verification_failed')
                    reused = True
                _tick(started, cancel_event)
                # Directory fsync makes the completed copy discoverable after restart.
                directory_fd = os.open(files, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
                manifest.append({'source_image': name, 'copy': 'files/' + target_name, 'bytes': count,
                                 'sha256': sha, 'verified': True, 'reused': reused, 'source_preserved': True})
            finally:
                temporary.unlink(missing_ok=True)
        _tick(started, cancel_event)
        identity = [{'source_image': row['source_image'], 'sha256': row['sha256']} for row in manifest]
        backup_id = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        result = {'status': 'completed', 'backup_id': backup_id, 'files': manifest, 'file_count': len(manifest),
                  'total_bytes': total, 'verified_copy_count': len(manifest), 'source_device_id': source_device,
                  'destination_device_id': destination_device, 'different_logical_filesystem': source_device != destination_device,
                  'physical_media_independence': 'unverified', 'independent_failure_protection_verified': False,
                  'storage_evidence': 'st_dev proves logical filesystem identity only; distinct partitions can share physical media.' if source_device != destination_device else 'Source and destination share the same filesystem; this is a verified second copy, not independent storage.',
                  'original_deleted': False, 'elapsed_seconds': round(time.monotonic() - started, 3),
                  'receipt': f'camera-autopilot-backup/receipts/{backup_id}.json'}
        receipt = receipts / (backup_id + '.json')
        if receipt.exists() or receipt.is_symlink():
            if receipt.is_symlink() or not receipt.is_file() or receipt.stat().st_size > 1024 * 1024:
                raise BackupError('Existing backup receipt is unsafe.', 'invalid_destination')
            recorded = json.loads(receipt.read_text())
            fields = ('source_image', 'copy', 'bytes', 'sha256')
            try:
                old_rows = [{key: row[key] for key in fields} for row in recorded['files']]
                new_rows = [{key: row[key] for key in fields} for row in manifest]
            except (KeyError, TypeError):
                raise BackupError('Existing backup receipt is malformed.', 'verification_failed')
            if recorded.get('backup_id') != backup_id or old_rows != new_rows:
                raise BackupError('Existing backup receipt identity mismatched.', 'verification_failed')
            result['receipt_reused'] = True
        else:
            payload = json.dumps(result, indent=2)
            pending_receipt = receipts / ('.partial-' + uuid.uuid4().hex)
            try:
                receipt_fd = os.open(pending_receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(receipt_fd, 'w') as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.link(pending_receipt, receipt, follow_symlinks=False)
            finally:
                pending_receipt.unlink(missing_ok=True)
            directory_fd = os.open(receipts, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return result
