"""Signed offline software updates in an explicitly initialized disposable root.

This tool never starts services, installs dependencies, runs package code, or
reboots. The atomic current.json pointer is a sandbox activation contract only.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import struct
import tempfile
import uuid
import zipfile
import zlib

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

MAX_FILES = 4096
MAX_FILE_BYTES = 128 * 1024 * 1024
MAX_TOTAL_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_COMPRESSION_RATIO = 500
MAX_CENTRAL_DIRECTORY_BYTES = 4 * 1024 * 1024
ROOT_SCHEMA = 'camera-disposable-update-root-v1'
BUNDLE_SCHEMA = 'camera-offline-update-v1'


class UpdateError(ValueError):
    pass


class ActivationUncertain(UpdateError):
    """Pointer may already have changed; inspect before deciding anything else."""


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise UpdateError('Duplicate JSON key')
        result[key] = value
    return result


def _json(raw):
    try:
        return json.loads(raw, object_pairs_hook=_pairs)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError('Invalid JSON') from exc


def _read(path: Path, maximum: int) -> bytes:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
        raise UpdateError('Expected a bounded regular file')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise UpdateError('File exceeds size bound')
    return data


def _key(raw):
    try:
        key = serialization.load_pem_public_key(raw)
    except (ValueError, TypeError) as exc:
        raise UpdateError('Invalid pinned public key') from exc
    if not isinstance(key, Ed25519PublicKey):
        raise UpdateError('Pinned key must be Ed25519')
    return key


def _sync(directory):
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _create(path, raw, mode=0o600):
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(descriptor, 'wb') as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _write_state(root, state):
    temporary = root / ('.state-' + uuid.uuid4().hex + '.tmp')
    _create(temporary, json.dumps(state, sort_keys=True).encode())
    try:
        os.replace(temporary, root / 'current.json')
        try:
            _sync(root)
        except OSError as exc:
            raise ActivationUncertain('Pointer replaced but directory sync failed; inspect current.json') from exc
    finally:
        if temporary.exists():
            temporary.unlink()


def initialize(root: Path, public_key: Path):
    """Create NEW private root; it cannot adopt an existing directory."""
    root = root.absolute()
    if root.parent.resolve() != root.parent:
        raise UpdateError('Root parent must not contain symlinks')
    raw_key = _read(public_key, 16384)
    _key(raw_key)
    root.mkdir(mode=0o700, parents=False, exist_ok=False)
    (root / 'versions').mkdir(mode=0o700)
    _create(root / 'trusted-public.pem', raw_key, 0o400)
    marker = {'schema': ROOT_SCHEMA, 'root': str(root),
              'key_sha256': hashlib.sha256(raw_key).hexdigest()}
    _create(root / '.disposable-update-root.json', json.dumps(marker).encode())
    _create(root / '.update.lock', b'')
    _write_state(root, {'schema': 1, 'active': None, 'previous': None,
                        'max_version': 0, 'generation': 0, 'last_operation': 'initialize'})
    return {'initialized': True, 'root': str(root), 'services_changed': False,
            'shipping_key_verified': False}


@contextmanager
def _locked(root: Path):
    root = root.absolute()
    info = root.lstat()
    if (root.resolve() != root or not stat.S_ISDIR(info.st_mode) or
            info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
        raise UpdateError('Use an owned private initialized disposable root')
    marker = _json(_read(root / '.disposable-update-root.json', 16384))
    if not isinstance(marker, dict) or marker.get('schema') != ROOT_SCHEMA or marker.get('root') != str(root):
        raise UpdateError('Disposable-root identity mismatch')
    public = _read(root / 'trusted-public.pem', 16384)
    if hashlib.sha256(public).hexdigest() != marker.get('key_sha256'):
        raise UpdateError('Pinned key differs from initialized root')
    versions = root / 'versions'
    if versions.is_symlink() or not versions.is_dir():
        raise UpdateError('Invalid version directory')
    descriptor = os.open(root / '.update.lock', os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise UpdateError('Invalid update lock')
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield root, _key(public)
    except BlockingIOError as exc:
        raise UpdateError('Another update operation is running') from exc
    finally:
        os.close(descriptor)


def _relative(value):
    if not isinstance(value, str) or not value or len(value.encode()) > 256:
        raise UpdateError('Invalid package path')
    path = PurePosixPath(value)
    if (value == '.' or path.is_absolute() or str(path) != value or '..' in path.parts or
            '\\' in value or any(ord(c) < 32 for c in value)):
        raise UpdateError('Package path escapes or is noncanonical')
    return value


def _manifest(raw, signature, key):
    if len(raw) > MAX_MANIFEST_BYTES or len(signature) != 64:
        raise UpdateError('Manifest/signature size invalid')
    try:
        key.verify(signature, raw)
    except InvalidSignature as exc:
        raise UpdateError('Signature does not match pinned public key') from exc
    value = _json(raw)
    if not isinstance(value, dict) or value.get('schema') != BUNDLE_SCHEMA:
        raise UpdateError('Unsupported update schema')
    version = value.get('version')
    if type(version) is not int or not 1 <= version <= 2**53 - 1:
        raise UpdateError('Version must be a positive monotonic integer')
    files = value.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= MAX_FILES:
        raise UpdateError('Invalid update file count')
    total = 0
    for name, metadata in files.items():
        _relative(name)
        if not isinstance(metadata, dict):
            raise UpdateError('Invalid file metadata')
        size, digest = metadata.get('size_bytes'), metadata.get('sha256')
        if type(size) is not int or not 0 <= size <= MAX_FILE_BYTES:
            raise UpdateError('Invalid payload size')
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            raise UpdateError('Invalid payload SHA256')
        total += size
    if total > MAX_TOTAL_BYTES:
        raise UpdateError('Payload exceeds total limit')
    return value


def _stream_hash(stream, expected, destination=None):
    digest, total = hashlib.sha256(), 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            break
        total += len(chunk)
        if total > expected['size_bytes']:
            raise UpdateError('Payload exceeds declared size')
        digest.update(chunk)
        if destination is not None:
            destination.write(chunk)
    if total != expected['size_bytes'] or digest.hexdigest() != expected['sha256']:
        raise UpdateError('Payload hash or size mismatch')



def _preflight_zip(stream, size):
    """Bound central-directory work before ZipFile allocates its entry objects.

    This software-only format needs neither ZIP64 nor split archives. Parse fixed
    headers in constant memory and confirm the declared entry count is truthful.
    """
    tail_size = min(size, 22 + 65535)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    index = tail.rfind(b'PK\x05\x06')
    if index < 0 or index + 22 > len(tail):
        raise UpdateError('ZIP end record missing')
    fields = struct.unpack('<4s4H2IH', tail[index:index + 22])
    _, disk, central_disk, disk_entries, entries, central_bytes, central_offset, comment_bytes = fields
    end_offset = size - tail_size + index
    if index + 22 + comment_bytes != len(tail):
        raise UpdateError('ZIP end record/comment length mismatch')
    if (disk or central_disk or disk_entries != entries or entries == 65535 or
            central_bytes == 0xffffffff or central_offset == 0xffffffff):
        raise UpdateError('ZIP64 or multi-disk archives are unsupported')
    if not 3 <= entries <= MAX_FILES + 2:
        raise UpdateError('ZIP declared entry count exceeds update format limit')
    if not entries * 46 <= central_bytes <= MAX_CENTRAL_DIRECTORY_BYTES:
        raise UpdateError('ZIP central directory exceeds byte budget')
    if central_offset + central_bytes != end_offset:
        raise UpdateError('ZIP central directory bounds are inconsistent')
    if end_offset >= 20:
        stream.seek(end_offset - 20)
        if stream.read(4) == b'PK\x06\x07':
            raise UpdateError('ZIP64 locator is unsupported')
    stream.seek(central_offset)
    cursor = central_offset
    for _ in range(entries):
        header = stream.read(46)
        if len(header) != 46 or header[:4] != b'PK\x01\x02':
            raise UpdateError('Invalid ZIP central directory entry')
        fields = struct.unpack('<4s6H3I5H2I', header)
        name_bytes, extra_bytes, note_bytes, start_disk = fields[10:14]
        if (fields[2] >= 45 or not 1 <= name_bytes <= 256 or extra_bytes > 4096 or note_bytes > 4096 or
                start_disk or fields[8] == 0xffffffff or fields[9] == 0xffffffff or
                fields[16] == 0xffffffff):
            raise UpdateError('ZIP entry metadata exceeds supported bounds')
        cursor += 46 + name_bytes + extra_bytes + note_bytes
        if cursor > end_offset:
            raise UpdateError('ZIP directory entry escapes declared directory')
        stream.seek(cursor)
    if cursor != end_offset:
        raise UpdateError('ZIP central directory count differs from end record')
    stream.seek(0)


@contextmanager
def _private_archive_snapshot(archive):
    """Bound and verify incoming bytes before any ZIP structure is allocated.

    The anonymous temporary descriptor is not the mutable incoming pathname.
    Source identity and a second hash pass must agree before parsing begins.
    This is not protection against an attacker controlling the running account.
    """
    descriptor = os.open(archive, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as incoming:
        before = os.fstat(incoming.fileno())
        limit = MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES * 2
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= limit:
            raise UpdateError('Archive must be a bounded regular file')
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size,
                                 info.st_mtime_ns, info.st_ctime_ns)
        with tempfile.TemporaryFile(mode='w+b', prefix='camera-update-snapshot-') as snapshot:
            digest, count = hashlib.sha256(), 0
            while True:
                chunk = incoming.read(min(1024 * 1024, before.st_size + 1 - count))
                if not chunk:
                    break
                count += len(chunk)
                if count > before.st_size or count > limit:
                    raise UpdateError('Incoming archive grew while snapshotting')
                snapshot.write(chunk)
                digest.update(chunk)
            after = os.fstat(incoming.fileno())
            if count != before.st_size or identity(before) != identity(after):
                raise UpdateError('Incoming archive changed while snapshotting')
            incoming.seek(0)
            check, checked = hashlib.sha256(), 0
            while True:
                chunk = incoming.read(min(1024 * 1024, before.st_size + 1 - checked))
                if not chunk:
                    break
                checked += len(chunk)
                if checked > before.st_size:
                    raise UpdateError('Incoming archive changed during snapshot hash check')
                check.update(chunk)
            if (checked != count or check.digest() != digest.digest() or
                    identity(before) != identity(os.fstat(incoming.fileno()))):
                raise UpdateError('Incoming archive identity or hash changed during snapshot verification')
            snapshot.flush()
            os.fchmod(snapshot.fileno(), 0o400)
            snapshot.seek(0)
            yield snapshot, count


@contextmanager
def _checked_zip(archive):
    # Preflight and parser see only this private snapshot, never the incoming
    # pathname/descriptor that another process may still be modifying in place.
    with _private_archive_snapshot(archive) as (stream, size):
        _preflight_zip(stream, size)
        with zipfile.ZipFile(stream) as zipped:
            yield zipped


def _archive(archive, key, extract=None):
    info = archive.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES * 2:
        raise UpdateError('Invalid or oversized archive')
    try:
        with _checked_zip(archive) as zipped:
            entries = zipped.infolist()
            names = [entry.filename for entry in entries]
            if len(entries) > MAX_FILES + 2 or len(set(names)) != len(names):
                raise UpdateError('Duplicate archive entries or excessive count')
            for entry in entries:
                _relative(entry.filename)
                kind = stat.S_IFMT(entry.external_attr >> 16)
                if (entry.is_dir() or kind not in (0, stat.S_IFREG) or entry.flag_bits & 1 or
                        entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                    raise UpdateError('Unsupported, encrypted, directory or symlink archive entry')
                if entry.file_size > MAX_FILE_BYTES or entry.file_size / max(1, entry.compress_size) > MAX_COMPRESSION_RATIO:
                    raise UpdateError('Archive member exceeds size/compression limits')
            raw_info, signature_info = zipped.getinfo('manifest.json'), zipped.getinfo('manifest.sig')
            if raw_info.file_size > MAX_MANIFEST_BYTES or signature_info.file_size != 64:
                raise UpdateError('Invalid manifest/signature archive size')
            raw, signature = zipped.read(raw_info), zipped.read(signature_info)
            manifest = _manifest(raw, signature, key)
            expected_names = {'manifest.json', 'manifest.sig'} | {'payload/' + name for name in manifest['files']}
            if set(names) != expected_names:
                raise UpdateError('Archive file set differs from signed manifest')
            if extract is not None:
                (extract / 'payload').mkdir(mode=0o700)
                _create(extract / 'manifest.json', raw, 0o400)
                _create(extract / 'manifest.sig', signature, 0o400)
            for name, expected in manifest['files'].items():
                member = zipped.getinfo('payload/' + name)
                if member.file_size != expected['size_bytes']:
                    raise UpdateError('Archive member size differs from signed manifest')
                with zipped.open(member) as stream:
                    if extract is None:
                        _stream_hash(stream, expected)
                    else:
                        destination = extract / 'payload' / name
                        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
                        with os.fdopen(descriptor, 'wb') as target:
                            _stream_hash(stream, expected, target)
                            target.flush()
                            os.fsync(target.fileno())
            return manifest, hashlib.sha256(raw).hexdigest()
    except (zipfile.BadZipFile, KeyError, RuntimeError, zlib.error) as exc:
        raise UpdateError('Invalid or incomplete archive') from exc


def verify(archive: Path, public_key: Path):
    manifest, digest = _archive(archive, _key(_read(public_key, 16384)))
    return {'verified': True, 'version': manifest['version'], 'manifest_sha256': digest,
            'file_count': len(manifest['files']), 'executed': False, 'downloaded': False}


def _version_id(version, digest):
    return f'{version}-{digest}'


def _staged(root, identifier, key):
    if not isinstance(identifier, str) or not identifier or '/' in identifier or '\\' in identifier:
        raise UpdateError('Invalid version identifier')
    directory = root / 'versions' / identifier
    if directory.is_symlink() or not directory.is_dir():
        raise UpdateError('Staged version unavailable')
    raw = _read(directory / 'manifest.json', MAX_MANIFEST_BYTES)
    manifest = _manifest(raw, _read(directory / 'manifest.sig', 64), key)
    if identifier != _version_id(manifest['version'], hashlib.sha256(raw).hexdigest()):
        raise UpdateError('Version directory identity differs from manifest')
    expected_files = {'manifest.json', 'manifest.sig'} | {'payload/' + name for name in manifest['files']}
    actual = set()
    for path in directory.rglob('*'):
        if path.is_symlink():
            raise UpdateError('Staged symlink rejected')
        if path.is_file():
            actual.add(path.relative_to(directory).as_posix())
        elif not path.is_dir():
            raise UpdateError('Nonregular staged entry rejected')
    if actual != expected_files:
        raise UpdateError('Staged file set differs from manifest')
    for name, expected in manifest['files'].items():
        path = directory / 'payload' / name
        if path.stat().st_size != expected['size_bytes']:
            raise UpdateError('Staged file size mismatch')
        with path.open('rb') as stream:
            _stream_hash(stream, expected)
    return manifest


def _state(root):
    state = _json(_read(root / 'current.json', 16384))
    if (not isinstance(state, dict) or state.get('schema') != 1 or
            type(state.get('max_version')) is not int or state['max_version'] < 0 or
            type(state.get('generation')) is not int or state['generation'] < 0):
        raise UpdateError('Invalid activation state; do not infer activation from staged directories')
    for name in ('active', 'previous'):
        value = state.get(name)
        if value is not None and (not isinstance(value, str) or '/' in value or '\\' in value):
            raise UpdateError('Invalid activation pointer')
    return state


def stage(root: Path, archive: Path, *, apply=False):
    with _locked(root) as (root, key):
        manifest, digest = _archive(archive, key)
        state = _state(root)
        version = manifest['version']
        if version < state['max_version']:
            raise UpdateError('Antirollback: package version is below highest activated version')
        identifier = _version_id(version, digest)
        target = root / 'versions' / identifier
        # One signed payload identity per version, even if another bundle has a valid signature.
        conflicts = [p for p in (root / 'versions').glob(f'{version}-*') if p.name != identifier]
        if conflicts:
            raise UpdateError('Version number already belongs to a different signed payload')
        if target.exists():
            _staged(root, identifier, key)
            # A prior rename may have succeeded while syncing its directory
            # failed. Idempotent apply must repair that durability boundary.
            if apply:
                _sync(target.parent)
            return {'staged': True, 'idempotent': True, 'version_id': identifier,
                    'applied': False, 'durability_verified': apply}
        if not apply:
            return {'staged': False, 'dry_run': True, 'version_id': identifier}
        temporary = root / 'versions' / ('.stage-' + uuid.uuid4().hex)
        temporary.mkdir(mode=0o700)
        try:
            second, second_digest = _archive(archive, key, temporary)
            if second_digest != digest:
                raise UpdateError('Archive changed between verification and staging')
            for directory in sorted([p for p in temporary.rglob('*') if p.is_dir()], key=lambda p: len(p.parts), reverse=True):
                _sync(directory)
                directory.chmod(0o500)
            _sync(temporary)
            temporary.chmod(0o500)
            os.rename(temporary, target)
            _sync(target.parent)
        except BaseException:
            if temporary.exists():
                _remove_temporary(temporary)
            raise
        return {'staged': True, 'idempotent': False, 'version_id': identifier, 'applied': True}


def _remove_temporary(path):
    # Only this invocation's random private staging directory, never a published version.
    for directory in [path] + [p for p in path.rglob('*') if p.is_dir() and not p.is_symlink()]:
        directory.chmod(0o700)
    shutil.rmtree(path)


def activate(root: Path, identifier: str, *, apply=False):
    with _locked(root) as (root, key):
        manifest = _staged(root, identifier, key)
        state = _state(root)
        if state['active'] == identifier:
            # Reading the new pointer does not prove the previous directory
            # sync completed. A requested retry must retry that sync too.
            if apply:
                try:
                    _sync(root)
                except OSError as exc:
                    raise ActivationUncertain('Active pointer present but directory sync still failed; inspect current.json') from exc
            return {'activated': True, 'idempotent': True, 'state': state,
                    'applied': False, 'durability_verified': apply}
        if manifest['version'] <= state['max_version']:
            raise UpdateError('Antirollback: activate requires a newer version; use explicit rollback to previous')
        if not apply:
            return {'activated': False, 'dry_run': True, 'version_id': identifier}
        updated = dict(state, previous=state['active'], active=identifier,
                       max_version=manifest['version'], generation=state['generation'] + 1,
                       last_operation='activate')
        _write_state(root, updated)
        return {'activated': True, 'idempotent': False, 'state': updated, 'applied': True,
                'services_changed': False}


def rollback(root: Path, *, apply=False):
    with _locked(root) as (root, key):
        state = _state(root)
        if not state['previous']:
            raise UpdateError('No previous activation is available for explicit rollback')
        _staged(root, state['previous'], key)
        if not apply:
            return {'rolled_back': False, 'dry_run': True, 'version_id': state['previous']}
        updated = dict(state, active=state['previous'], previous=state['active'],
                       generation=state['generation'] + 1, last_operation='operator-rollback')
        _write_state(root, updated)
        return {'rolled_back': True, 'state': updated, 'applied': True, 'services_changed': False}


def inspect(root: Path):
    """Recover by inspecting the authoritative pointer; never guess/promote a version."""
    with _locked(root) as (root, key):
        state = _state(root)
        if state['active']:
            manifest = _staged(root, state['active'], key)
            if manifest['version'] > state['max_version']:
                raise UpdateError('Activation exceeds antirollback watermark')
        return {'state': state, 'active_verified': bool(state['active']),
                'orphaned_temporary_entries': sorted(
                    [p.name for p in root.glob('.state-*.tmp')] +
                    [p.name for p in (root / 'versions').glob('.stage-*')]),
                'recovery': 'Authoritative current.json preserved; temporary entries are never activated',
                'physical_power_loss_verified': False, 'services_changed': False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['initialize', 'verify', 'stage', 'activate', 'rollback', 'inspect'])
    parser.add_argument('--root', type=Path)
    parser.add_argument('--archive', type=Path)
    parser.add_argument('--public-key', type=Path)
    parser.add_argument('--version-id')
    parser.add_argument('--apply', action='store_true', help='Mutate only an explicitly initialized disposable root')
    args = parser.parse_args(argv)
    try:
        if args.operation == 'verify':
            if not args.archive or not args.public_key:
                parser.error('verify requires --archive and --public-key')
            result = verify(args.archive, args.public_key)
        else:
            if not args.root:
                parser.error('--root is required')
            if args.operation == 'initialize':
                if not args.public_key:
                    parser.error('initialize requires --public-key')
                result = initialize(args.root, args.public_key) if args.apply else {'dry_run': True, 'initialized': False}
            elif args.operation == 'stage':
                if not args.archive:
                    parser.error('stage requires --archive')
                result = stage(args.root, args.archive, apply=args.apply)
            elif args.operation == 'activate':
                if not args.version_id:
                    parser.error('activate requires --version-id')
                result = activate(args.root, args.version_id, apply=args.apply)
            elif args.operation == 'rollback':
                result = rollback(args.root, apply=args.apply)
            else:
                result = inspect(args.root)
        print(json.dumps(result, indent=2))
        return 0
    except (UpdateError, OSError) as exc:
        print(json.dumps({'error': str(exc), 'inspect_required': isinstance(exc, ActivationUncertain),
                          'services_changed': False}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
