"""Durable, bounded external-event/file ingestion; never issues a shutter command.

Only the fixture adapter is enabled. Actual camera/accessory event transport must
be proven separately before any native adapter can be added. Accepted-frame
receipts match stack_session.frame_receipt for camera-free rendering.
"""
from __future__ import annotations

from contextlib import contextmanager
from fractions import Fraction
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import time
import uuid

from stack_session import frame_receipt

MAX_EVENTS = 240
MAX_SESSIONS = 100
MAX_FRAME_BYTES = 64 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_COPY_SECONDS = 30
EVENT_LIFETIME_SECONDS = 3600
MODELS = {'Sony ILCE-7M2 (Control)': 'ILCE-7M2', 'Sony Alpha-A7 III (PC Control)': 'ILCE-7M3'}


class ExternalTriggerError(ValueError):
    def __init__(self, message, reason='invalid_event'):
        super().__init__(message)
        self.reason = reason
        self.retry_capture = False


class IngestStateUncertain(ExternalTriggerError):
    """Inspect this session; an atomic replacement may already have completed."""


def _regular_open(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        raise ExternalTriggerError('Input must be a regular file.', 'invalid_source')
    return fd, info


def _sync(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic(path, value):
    temporary = path.with_name('.state-' + uuid.uuid4().hex + '.tmp')
    try:
        with os.fdopen(os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'w') as stream:
            json.dump(value, stream, allow_nan=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            _sync(path.parent)
        except OSError as error:
            raise IngestStateUncertain('State replaced but directory sync failed; inspect this ingest session.', 'state_uncertain') from error
    finally:
        temporary.unlink(missing_ok=True)


def _private_directory(path, create=False):
    if create:
        try:
            path.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or
            info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise ExternalTriggerError('Ingest directory must be owned, private and not a symlink.', 'invalid_root')


def _camera(value):
    if not isinstance(value, dict) or set(value) != {'model', 'port'}:
        raise ExternalTriggerError('Exact camera model and port are required.', 'camera_mismatch')
    if not isinstance(value['model'], str) or value['model'] not in MODELS or not isinstance(value['port'], str) or not re.fullmatch(r'usb:[0-9]{3},[0-9]{3}', value['port']):
        raise ExternalTriggerError('Select a supported exact USB identity.', 'camera_mismatch')
    return dict(value)


def _exposure(value):
    if not isinstance(value, dict) or set(value) != {'iso', 'aperture', 'shutter'}:
        raise ExternalTriggerError('Exposure needs ISO, aperture and shutter.', 'invalid_exposure')
    result = {}
    for name, low, high in [('iso', 1, 409600), ('aperture', .5, 128), ('shutter', 1 / 64000, 600)]:
        supplied = value[name]
        if isinstance(supplied, bool) or not isinstance(supplied, (int, float, str)) or len(str(supplied)) > 32:
            raise ExternalTriggerError('Invalid exposure value.', 'invalid_exposure')
        try:
            number = float(Fraction(str(supplied)))
        except (ValueError, ZeroDivisionError, OverflowError):
            raise ExternalTriggerError('Invalid exposure value.', 'invalid_exposure')
        if not math.isfinite(number) or not low <= number <= high:
            raise ExternalTriggerError('Exposure outside supported bounds.', 'invalid_exposure')
        result[name] = round(number, 9)
    return result


def _same_exposure(left, right):
    return all(math.isclose(left[name], right[name], rel_tol=.01, abs_tol=1e-9) for name in ('iso', 'aperture', 'shutter'))


def _jpeg_exposure(path, model):
    from image_safety import open_image
    with open_image(path) as image:
        exif = image.getexif()
        camera_model = str(exif.get(272, '')).strip()
        if camera_model and camera_model != MODELS[model]:
            raise ExternalTriggerError('JPEG model differs from the event camera.', 'camera_mismatch')
        sub = exif.get_ifd(34665)
        values = {}
        for name, tag in [('iso', 34855), ('aperture', 33437), ('shutter', 33434)]:
            value = sub.get(tag, exif.get(tag))
            if value is None:
                raise ExternalTriggerError('JPEG lacks exposure EXIF; frame remains unaccepted.', 'exposure_unverified')
            if isinstance(value, tuple):
                value = value[0]
            values[name] = float(value)
    return _exposure(values)


class ExternalTriggerIngest:
    def __init__(self, root):
        path = Path(root).absolute()
        if path.parent.resolve() != path.parent:
            raise ExternalTriggerError('Ingest root parent cannot contain symlinks.', 'invalid_root')
        _private_directory(path, create=True)
        self.root = path
        fd = os.open(path / '.root.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ExternalTriggerError('Invalid root lock.')
        finally:
            os.close(fd)

    def _directory(self, session_id):
        if not isinstance(session_id, str) or not re.fullmatch(r'[0-9a-f]{32}', session_id):
            raise ExternalTriggerError('Invalid ingest session id.')
        directory = self.root / session_id
        _private_directory(directory)
        return directory

    @contextmanager
    def _locked(self, session_id):
        directory = self._directory(session_id)
        fd = os.open(directory / '.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ExternalTriggerError('Invalid ingest lock.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ExternalTriggerError('Another ingest operation is active.', 'busy') from error
            descriptor, info = _regular_open(directory / 'state.json')
            with os.fdopen(descriptor, 'rb') as stream:
                if info.st_size > MAX_MANIFEST_BYTES:
                    raise ExternalTriggerError('Ingest manifest exceeds its bound.')
                state = json.loads(stream.read(MAX_MANIFEST_BYTES + 1))
            if (not isinstance(state, dict) or state.get('id') != session_id or state.get('schema') != 1 or
                    state.get('adapter') != 'fixture' or not isinstance(state.get('events'), dict) or
                    len(state['events']) > MAX_EVENTS):
                raise ExternalTriggerError('Ingest manifest is invalid; do not infer events from loose files.')
            yield directory, state
        finally:
            os.close(fd)

    def _save(self, directory, state):
        state['updated_at'] = time.time()
        _atomic(directory / 'state.json', state)

    def create(self, model, port, frame_count, *, adapter='fixture', settle_seconds=.25):
        fd = os.open(self.root / '.root.lock', os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise ExternalTriggerError('Invalid root lock.')
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ExternalTriggerError('Another ingest creation is active.', 'busy') from error
            return self._create(model, port, frame_count, adapter=adapter, settle_seconds=settle_seconds)
        finally:
            os.close(fd)

    def _create(self, model, port, frame_count, *, adapter='fixture', settle_seconds=.25):
        if adapter != 'fixture':
            raise ExternalTriggerError('Native/accessory external-trigger transport is unverified and disabled.', 'native_route_unverified')
        camera = _camera({'model': model, 'port': port})
        if type(frame_count) is not int or not 1 <= frame_count <= MAX_EVENTS:
            raise ExternalTriggerError('Frame count must be 1–240.')
        if isinstance(settle_seconds, bool) or not isinstance(settle_seconds, (int, float)) or not .05 <= settle_seconds <= 10:
            raise ExternalTriggerError('Settle interval must be between .05 and 10 seconds.')
        if sum(1 for entry in self.root.iterdir() if entry.is_dir()) >= MAX_SESSIONS:
            raise ExternalTriggerError('Ingest session limit reached.', 'limit')
        session_id = uuid.uuid4().hex
        directory = self.root / session_id
        directory.mkdir(mode=0o700)
        os.close(os.open(directory / '.lock', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600))
        now = time.time()
        state = {'schema': 1, 'id': session_id, 'camera': camera, 'adapter': 'fixture',
                 'evidence': 'simulated external event/file adapter; no native transport or shutter control',
                 'frame_count': frame_count, 'settle_seconds': float(settle_seconds), 'events': {},
                 'created_at': now, 'updated_at': now, 'status': 'awaiting_events', 'retry_capture': False}
        self._save(directory, state)
        _sync(self.root)
        return state

    def read(self, session_id):
        with self._locked(session_id) as (_, state):
            return state

    @staticmethod
    def _event(state, event_id, camera):
        if _camera(camera) != state['camera']:
            raise ExternalTriggerError('Event belongs to a different camera.', 'camera_mismatch')
        if event_id not in state['events']:
            raise ExternalTriggerError('Observe the exact trigger event before supplying its file.', 'unknown_event')
        return state['events'][event_id]

    def observe_event(self, session_id, event_id, sequence, filename, sha256, camera):
        if (not isinstance(event_id, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', event_id) or
                not isinstance(filename, str) or len(filename) > 255 or Path(filename).name != filename or
                '\\' in filename or any(ord(c) < 32 for c in filename) or Path(filename).suffix.lower() not in {'.jpg', '.jpeg'} or
                not isinstance(sha256, str) or not re.fullmatch(r'[0-9a-f]{64}', sha256)):
            raise ExternalTriggerError('Event requires a bounded id, exact JPEG filename and SHA256.')
        with self._locked(session_id) as (directory, state):
            if _camera(camera) != state['camera']:
                raise ExternalTriggerError('Event belongs to a different camera.', 'camera_mismatch')
            if type(sequence) is not int or not 0 <= sequence < state['frame_count']:
                raise ExternalTriggerError('Event sequence exceeds this session.', 'limit')
            binding = {'id': event_id, 'sequence': sequence, 'filename': filename, 'sha256': sha256}
            if event_id in state['events']:
                existing = state['events'][event_id]
                if any(existing[key] != value for key, value in binding.items()):
                    raise ExternalTriggerError('Duplicate event id has conflicting file identity.', 'event_conflict')
                _sync(directory)  # Repair a previous post-replacement sync failure.
                return dict(existing, duplicate=True)
            if len(state['events']) >= state['frame_count'] or any(event['sequence'] == sequence for event in state['events'].values()):
                raise ExternalTriggerError('Sequence is already bound to another event.', 'event_conflict')
            if any(event['sha256'] == sha256 for event in state['events'].values()):
                raise ExternalTriggerError('The same file bytes are already bound to another trigger event.', 'duplicate_file')
            highest = max((event['sequence'] for event in state['events'].values()), default=-1)
            event = dict(binding, status='awaiting_file', late=sequence < highest, observed_at=time.time(),
                         file=None, exposure=None, stable_since=None, observations=0, retry_capture=False)
            state['events'][event_id] = event
            state['status'] = 'ingesting'
            self._save(directory, state)
            return dict(event, duplicate=False)

    @staticmethod
    def _alive(event):
        if time.time() - event['observed_at'] > EVENT_LIFETIME_SECONDS and event['status'] != 'accepted':
            raise ExternalTriggerError('External event expired; inspect it without issuing a replacement capture.', 'expired')

    def _existing_copy(self, directory, state, event):
        destination = directory / f"frame_{event['sequence']:04d}.jpg"
        if not destination.exists() and not destination.is_symlink():
            if event.get('file') or event['status'] == 'accepted':
                raise ExternalTriggerError('Previously ingested event file is missing; inspect this session.', 'missing_file')
            return None
        receipt = frame_receipt(destination)
        if receipt['sha256'] != event['sha256']:
            raise ExternalTriggerError('Stored event file hash changed; refusing reuse.', 'hash_mismatch')
        exposure = _jpeg_exposure(destination, state['camera']['model'])
        event['file'] = receipt
        event['image_exposure'] = exposure
        return receipt

    def supply_file(self, session_id, event_id, source, camera, exposure):
        observed = _exposure(exposure)
        source = Path(source)
        with self._locked(session_id) as (directory, state):
            event = self._event(state, event_id, camera)
            self._alive(event)
            if source.name != event['filename']:
                raise ExternalTriggerError('Supplied filename differs from this event.', 'event_conflict')
            if event['status'] == 'accepted':
                self._existing_copy(directory, state, event)
                _sync(directory)
                return dict(event, duplicate=True)
            existing = self._existing_copy(directory, state, event)
            if existing and event['status'] == 'awaiting_settle':
                if not _same_exposure(observed, event['image_exposure']):
                    raise ExternalTriggerError('Duplicate file exposure conflicts with its JPEG.', 'exposure_mismatch')
                _sync(directory)
                return dict(event, duplicate=True)
            if not existing:
                if source.suffix.lower() not in {'.jpg', '.jpeg'}:
                    raise ExternalTriggerError('Only exact JPEG event files are accepted.')
                descriptor, before = _regular_open(source)
                pending = directory / f".frame_{event['sequence']:04d}.pending.jpg"
                with os.fdopen(descriptor, 'rb') as incoming:
                    if not 0 < before.st_size <= MAX_FRAME_BYTES:
                        raise ExternalTriggerError('Ingress file exceeds byte budget.', 'limit')
                    # A file can be published before its receipt reaches the manifest.
                    # Reserve bytes durably before copying, so restart cannot hide it
                    # from the next event's quota calculation.
                    total = sum(max((item.get('file') or {}).get('bytes', 0), item.get('reserved_bytes', 0))
                                for item in state['events'].values() if item['id'] != event_id)
                    if total + before.st_size > MAX_TOTAL_BYTES:
                        raise ExternalTriggerError('Ingest session exceeds total byte budget.', 'limit')
                    event['status'] = 'copying'
                    event['reserved_bytes'] = before.st_size
                    self._save(directory, state)  # Durable event intent precedes bytes.
                    # Only this event's private interrupted copy, never the supplied source.
                    pending.unlink(missing_ok=True)
                    started = time.monotonic()
                    try:
                        digest, count = hashlib.sha256(), 0
                        with os.fdopen(os.open(pending, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600), 'wb') as target:
                            while chunk := incoming.read(1024 * 1024):
                                count += len(chunk)
                                if count > before.st_size or time.monotonic() - started > MAX_COPY_SECONDS:
                                    raise ExternalTriggerError('Ingress changed or exceeded copy deadline.', 'limit')
                                target.write(chunk)
                                digest.update(chunk)
                            target.flush()
                            os.fsync(target.fileno())
                        after = os.fstat(incoming.fileno())
                        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
                        if count != before.st_size or identity(before) != identity(after) or identity(after) != identity(source.stat(follow_symlinks=False)):
                            raise ExternalTriggerError('Source changed during ingress.', 'source_changed')
                        if digest.hexdigest() != event['sha256']:
                            raise ExternalTriggerError('Supplied file hash differs from the event.', 'hash_mismatch')
                        receipt = frame_receipt(pending)
                        image_exposure = _jpeg_exposure(pending, state['camera']['model'])
                        if not _same_exposure(image_exposure, observed):
                            raise ExternalTriggerError('Exposure observation differs from the exact JPEG EXIF.', 'exposure_mismatch')
                        destination = directory / f"frame_{event['sequence']:04d}.jpg"
                        os.link(pending, destination, follow_symlinks=False)
                        _sync(directory)
                        receipt['path'] = str(destination)
                        event.update(file=receipt, image_exposure=image_exposure,
                                     source={'filename': source.name, 'sha256': digest.hexdigest(), 'bytes': count,
                                             'device_id': before.st_dev, 'inode': before.st_ino, 'preserved': True})
                    except Exception as error:
                        event['last_error'] = {'reason': getattr(error, 'reason', 'file_ingress_failed'),
                                               'message': str(error)[:512], 'retry_capture': False}
                        self._save(directory, state)
                        raise
                    finally:
                        pending.unlink(missing_ok=True)
            if not _same_exposure(observed, event['image_exposure']):
                raise ExternalTriggerError('Exposure differs from recovered JPEG.', 'exposure_mismatch')
            event.pop('last_error', None)
            event.update(status='awaiting_settle', exposure=observed, stable_since=time.time(), observations=1)
            self._save(directory, state)
            return dict(event, duplicate=False)

    def observe_exposure(self, session_id, event_id, camera, exposure):
        observed = _exposure(exposure)
        with self._locked(session_id) as (directory, state):
            event = self._event(state, event_id, camera)
            self._alive(event)
            if event['status'] == 'accepted':
                self._existing_copy(directory, state, event)
                _sync(directory)
                return dict(event, duplicate=True)
            if event['status'] != 'awaiting_settle' or not event.get('file'):
                raise ExternalTriggerError('Supply the exact event file before exposure settling.', 'awaiting_file')
            self._existing_copy(directory, state, event)
            now = time.time()
            if event['exposure'] != observed or now < event['stable_since']:
                event.update(exposure=observed, stable_since=now, observations=1)
            else:
                event['observations'] += 1
            matches = _same_exposure(observed, event['image_exposure'])
            event['exposure_matches_image'] = matches
            if matches and event['observations'] >= 2 and now - event['stable_since'] >= state['settle_seconds']:
                event.update(status='accepted', accepted_at=now)
                if len(state['events']) == state['frame_count'] and all(item['status'] == 'accepted' for item in state['events'].values()):
                    state['status'] = 'completed'
            self._save(directory, state)
            return dict(event, duplicate=False)

    def render_inputs(self, session_id):
        """Return a complete ordered accepted-frame contract; never invoke capture.

        Call stack_session.merge_session_frames(mode, [f['receipt']['path'] for f
        in frames], output_path) with this result for a camera-free JPEG render.
        """
        with self._locked(session_id) as (directory, state):
            ordered = sorted(state['events'].values(), key=lambda event: event['sequence'])
            if len(ordered) != state['frame_count'] or any(event['status'] != 'accepted' or event['sequence'] != index for index, event in enumerate(ordered)):
                raise ExternalTriggerError('Rendering requires every exact event file to be accepted.', 'incomplete')
            frames = []
            for event in ordered:
                receipt = self._existing_copy(directory, state, event)
                frames.append({'index': event['sequence'], 'status': 'accepted', 'receipt': receipt,
                               'capture_started_at': None, 'external_event_observed_at': event['observed_at'],
                               'camera': dict(state['camera']), 'applied': {'external_trigger': True,
                               'exposure': event['image_exposure'], 'adapter': 'fixture', 'native_route_verified': False},
                               'event_id': event['id']})
            return frames
