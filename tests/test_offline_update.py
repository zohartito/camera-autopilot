"""Disposable roots and ephemeral test keys; no deployment or shipping identity."""
import hashlib
import json
from pathlib import Path
import stat
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from deploy import offline_update as update


@pytest.fixture
def signing(tmp_path):
    private = Ed25519PrivateKey.generate()
    public = tmp_path / 'test-public.pem'
    public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def bundle(tmp_path, private, version=1, files=None, *, suffix='', metadata_changes=None):
    files = files if files is not None else {'backend/main.py': b'print("signed fixture, never executed")\n'}
    manifest = {'schema': update.BUNDLE_SCHEMA, 'version': version, 'files': {
        name: {'size_bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()} for name, raw in files.items()}}
    if metadata_changes:
        metadata_changes(manifest)
    raw = json.dumps(manifest, sort_keys=True).encode()
    path = tmp_path / f'bundle-{version}{suffix}.zip'
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json', raw)
        archive.writestr('manifest.sig', private.sign(raw))
        for name, content in files.items():
            archive.writestr('payload/' + name, content)
    return path


def initialized(tmp_path, signing):
    root = tmp_path.resolve() / 'disposable-runtime'
    update.initialize(root, signing[1])
    return root


def publish(root, archive):
    staged = update.stage(root, archive, apply=True)
    update.activate(root, staged['version_id'], apply=True)
    return staged['version_id']


def rewrite(archive, modifier):
    with zipfile.ZipFile(archive) as source:
        entries = [(info, source.read(info)) for info in source.infolist()]
    with zipfile.ZipFile(archive, 'w') as target:
        for info, raw in entries:
            changed = modifier(info, raw)
            if changed is not None:
                target.writestr(*changed)


def flip_first_bit(raw):
    """Always changes the bytes; b'X' + raw[1:] was a no-op when raw already began with X."""
    return bytes([raw[0] ^ 0x01]) + raw[1:]


def test_signed_bundle_verifies_without_execution(tmp_path, signing):
    archive = bundle(tmp_path, signing[0])
    result = update.verify(archive, signing[1])
    assert result['verified'] and result['version'] == 1
    assert not result['executed'] and not result['downloaded']


def test_init_refuses_existing_root_and_preserves_key(tmp_path, signing):
    root = initialized(tmp_path, signing)
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    with pytest.raises(FileExistsError): update.initialize(root, signing[1])
    assert (root / 'trusted-public.pem').read_bytes() == signing[1].read_bytes()


def test_uninitialized_or_public_root_rejected(tmp_path, signing):
    with pytest.raises(OSError): update.inspect(tmp_path.resolve() / 'missing')
    root = initialized(tmp_path, signing)
    root.chmod(0o755)
    with pytest.raises(update.UpdateError): update.inspect(root)


def test_default_operations_are_dry_run(tmp_path, signing, capsys):
    root = tmp_path.resolve() / 'root'
    assert update.main(['initialize', '--root', str(root), '--public-key', str(signing[1])]) == 0
    assert not root.exists()
    root = initialized(tmp_path, signing)
    archive = bundle(tmp_path, signing[0])
    before = (root / 'current.json').read_bytes()
    result = update.stage(root, archive)
    assert result['dry_run'] and not list((root / 'versions').iterdir())
    staged = update.stage(root, archive, apply=True)
    assert update.activate(root, staged['version_id'])['dry_run']
    assert (root / 'current.json').read_bytes() == before


def test_stage_activate_idempotent_and_explicit_rollback_keeps_watermark(tmp_path, signing):
    root = initialized(tmp_path, signing)
    one = bundle(tmp_path, signing[0], 1)
    first = publish(root, one)
    assert update.stage(root, one, apply=True)['idempotent']
    assert update.activate(root, first, apply=True)['idempotent']
    second = publish(root, bundle(tmp_path, signing[0], 2))
    assert update.rollback(root)['dry_run']
    result = update.rollback(root, apply=True)
    assert result['state']['active'] == first
    assert result['state']['previous'] == second
    assert result['state']['max_version'] == 2
    with pytest.raises(update.UpdateError, match='Antirollback'): update.stage(root, one, apply=True)
    with pytest.raises(update.UpdateError, match='Antirollback'): update.activate(root, second, apply=True)
    assert update.inspect(root)['active_verified']


def test_no_previous_rollback_and_same_version_different_payload_rejected(tmp_path, signing):
    root = initialized(tmp_path, signing)
    with pytest.raises(update.UpdateError): update.rollback(root, apply=True)
    publish(root, bundle(tmp_path, signing[0]))
    conflict = bundle(tmp_path, signing[0], files={'other': b'different'}, suffix='other')
    with pytest.raises(update.UpdateError, match='different signed payload'): update.stage(root, conflict, apply=True)


@pytest.mark.parametrize('attack', ['payload', 'manifest', 'signature', 'extra', 'missing', 'duplicate'])
def test_tampered_exact_file_set_or_signature_rejected(tmp_path, signing, attack):
    archive = bundle(tmp_path, signing[0])
    if attack in {'payload', 'manifest', 'signature'}:
        target = {'payload': 'payload/backend/main.py', 'manifest': 'manifest.json', 'signature': 'manifest.sig'}[attack]
        rewrite(archive, lambda info, raw: (info, flip_first_bit(raw) if info.filename == target else raw))
    elif attack == 'missing':
        rewrite(archive, lambda info, raw: None if info.filename.startswith('payload/') else (info, raw))
    else:
        with zipfile.ZipFile(archive, 'a') as output:
            output.writestr('unexpected' if attack == 'extra' else 'manifest.sig', b'x' * 64)
    with pytest.raises(update.UpdateError): update.verify(archive, signing[1])


def test_signature_tamper_changes_a_signature_that_already_starts_with_x(tmp_path):
    with zipfile.ZipFile(bundle(tmp_path, Ed25519PrivateKey.generate(), suffix='probe')) as archive:
        manifest = archive.read('manifest.json')
    # Fixed seeds, fixed manifest bytes: the same key is found on every run.
    private = next(key for key in (Ed25519PrivateKey.from_private_bytes(seed.to_bytes(32, 'big'))
                                   for seed in range(1 << 16)) if key.sign(manifest)[:1] == b'X')
    public = tmp_path / 'x-public.pem'
    public.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo))
    archive = bundle(tmp_path, private)
    assert update.verify(archive, public)['verified']
    rewrite(archive, lambda info, raw: (info, flip_first_bit(raw) if info.filename == 'manifest.sig' else raw))
    with pytest.raises(update.UpdateError, match='Signature'): update.verify(archive, public)


def test_wrong_signer_and_changed_pinned_key_fail(tmp_path, signing):
    archive = bundle(tmp_path, Ed25519PrivateKey.generate())
    with pytest.raises(update.UpdateError, match='Signature'): update.verify(archive, signing[1])
    root = initialized(tmp_path, signing)
    (root / 'trusted-public.pem').chmod(0o600)
    (root / 'trusted-public.pem').write_bytes(b'substituted')
    with pytest.raises(update.UpdateError, match='Pinned key'): update.inspect(root)


@pytest.mark.parametrize('name', ['../escape', '/absolute', 'a/../../escape', 'a\\b', 'a//b', 'a/./b'])
def test_signed_traversal_paths_rejected(tmp_path, signing, name):
    archive = bundle(tmp_path, signing[0], files={name: b'escape'})
    with pytest.raises(update.UpdateError): update.verify(archive, signing[1])
    assert not (tmp_path.parent / 'escape').exists()


def test_archive_symlink_and_compression_bomb_rejected(tmp_path, signing):
    archive = bundle(tmp_path, signing[0])
    def symlink(info, raw):
        if info.filename.startswith('payload/'):
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        return info, raw
    rewrite(archive, symlink)
    with pytest.raises(update.UpdateError, match='symlink'): update.verify(archive, signing[1])
    bomb = bundle(tmp_path, signing[0], files={'zeros': b'0' * 200000}, suffix='bomb')
    with pytest.raises(update.UpdateError, match='compression'): update.verify(bomb, signing[1])


def test_declared_zip_size_and_limits_rejected(tmp_path, signing, monkeypatch):
    archive = bundle(tmp_path, signing[0], metadata_changes=lambda m: m['files']['backend/main.py'].update(size_bytes=1))
    with pytest.raises(update.UpdateError, match='size'): update.verify(archive, signing[1])
    archive = bundle(tmp_path, signing[0], suffix='bounded')
    monkeypatch.setattr(update, 'MAX_TOTAL_BYTES', 4)
    with pytest.raises(update.UpdateError): update.verify(archive, signing[1])


def test_tampered_staged_version_cannot_activate(tmp_path, signing):
    root = initialized(tmp_path, signing)
    identifier = update.stage(root, bundle(tmp_path, signing[0]), apply=True)['version_id']
    path = root / 'versions' / identifier / 'payload/backend/main.py'
    path.chmod(0o600)
    path.write_bytes(b'changed')
    with pytest.raises(update.UpdateError): update.activate(root, identifier, apply=True)
    assert update.inspect(root)['state']['active'] is None


def test_interruption_before_pointer_replace_keeps_old_activation(tmp_path, signing, monkeypatch):
    root = initialized(tmp_path, signing)
    first = publish(root, bundle(tmp_path, signing[0], 1))
    second = update.stage(root, bundle(tmp_path, signing[0], 2), apply=True)['version_id']
    def interrupt(*args): raise OSError('simulated interruption before atomic replacement')
    monkeypatch.setattr(update.os, 'replace', interrupt)
    with pytest.raises(OSError): update.activate(root, second, apply=True)
    report = update.inspect(root)
    assert report['state']['active'] == first and report['state']['max_version'] == 1
    assert report['active_verified']


def test_interruption_after_pointer_replace_requires_inspection_and_is_idempotent(tmp_path, signing, monkeypatch):
    root = initialized(tmp_path, signing)
    identifier = update.stage(root, bundle(tmp_path, signing[0]), apply=True)['version_id']
    original = update._sync
    calls = []
    def interrupt(path):
        calls.append(path)
        raise OSError('simulated directory sync failure')
    monkeypatch.setattr(update, '_sync', interrupt)
    with pytest.raises(update.ActivationUncertain): update.activate(root, identifier, apply=True)
    assert update.inspect(root)['state']['active'] == identifier
    with pytest.raises(update.ActivationUncertain): update.activate(root, identifier, apply=True)
    assert calls == [root, root]
    monkeypatch.setattr(update, '_sync', original)
    retried = update.activate(root, identifier, apply=True)
    assert retried['idempotent'] and retried['durability_verified']


def test_partial_stage_and_orphaned_state_never_become_activation(tmp_path, signing):
    root = initialized(tmp_path, signing)
    publish(root, bundle(tmp_path, signing[0]))
    (root / 'versions' / '.stage-interrupted').mkdir()
    (root / '.state-interrupted.tmp').write_text('{"active":"untrusted"}')
    result = update.inspect(root)
    assert result['active_verified']
    assert result['orphaned_temporary_entries'] == ['.stage-interrupted', '.state-interrupted.tmp']
    assert not result['physical_power_loss_verified']


def test_no_automatic_reconstruction_when_authoritative_state_corrupt(tmp_path, signing):
    root = initialized(tmp_path, signing)
    publish(root, bundle(tmp_path, signing[0]))
    (root / 'current.json').write_text('{}')
    with pytest.raises(update.UpdateError, match='do not infer'): update.inspect(root)


def test_interrupted_staging_never_promotes_partial_output(tmp_path, signing, monkeypatch):
    root = initialized(tmp_path, signing)
    archive = bundle(tmp_path, signing[0])
    original = update._archive
    def interrupt(path, key, extract=None):
        if extract is not None:
            (extract / 'partial').write_bytes(b'partial-copy')
            raise OSError('interrupted extraction')
        return original(path, key, extract)
    monkeypatch.setattr(update, '_archive', interrupt)
    with pytest.raises(OSError): update.stage(root, archive, apply=True)
    assert list((root / 'versions').iterdir()) == []
    assert update.inspect(root)['state']['active'] is None


def test_sync_failure_after_version_promotion_is_recovered_by_verified_retry(tmp_path, signing, monkeypatch):
    root = initialized(tmp_path, signing)
    archive = bundle(tmp_path, signing[0])
    original = update._sync
    def interrupt(path):
        if path == root / 'versions':
            raise OSError('directory sync interrupted after promotion')
        return original(path)
    monkeypatch.setattr(update, '_sync', interrupt)
    with pytest.raises(OSError): update.stage(root, archive, apply=True)
    assert update.inspect(root)['state']['active'] is None
    with pytest.raises(OSError): update.stage(root, archive, apply=True)
    monkeypatch.setattr(update, '_sync', original)
    retried = update.stage(root, archive, apply=True)
    assert retried['idempotent'] and retried['durability_verified']


def test_root_symlink_and_invalid_marker_rejected(tmp_path, signing):
    root = initialized(tmp_path, signing)
    alias = tmp_path / 'alias'
    alias.symlink_to(root)
    with pytest.raises(update.UpdateError): update.inspect(alias)
    (root / '.disposable-update-root.json').write_text('[]')
    with pytest.raises(update.UpdateError): update.inspect(root)


def test_concurrent_operation_fails_without_waiting(tmp_path, signing):
    root = initialized(tmp_path, signing)
    with update._locked(root):
        with pytest.raises(update.UpdateError, match='Another update'): update.inspect(root)


def test_idempotent_dry_run_does_not_claim_verified_durability(tmp_path, signing, monkeypatch):
    root = initialized(tmp_path, signing)
    archive = bundle(tmp_path, signing[0])
    identifier = publish(root, archive)
    def should_not_sync(*args): raise AssertionError('Dry run must not sync')
    monkeypatch.setattr(update, '_sync', should_not_sync)
    assert not update.stage(root, archive)['durability_verified']
    assert not update.activate(root, identifier)['durability_verified']


def test_excessive_directory_count_rejected_before_zipfile_allocates(tmp_path, signing, monkeypatch):
    # A genuine over-limit directory, still small enough for a bounded test fixture.
    archive = tmp_path / 'many.zip'
    with zipfile.ZipFile(archive, 'w') as output:
        for index in range(update.MAX_FILES + 3):
            output.writestr(f'{index:04d}', b'')
    def forbidden(*args, **kwargs): raise AssertionError('ZipFile must not allocate entries')
    monkeypatch.setattr(update.zipfile, 'ZipFile', forbidden)
    with pytest.raises(update.UpdateError, match='entry count'):
        update.verify(archive, signing[1])


@pytest.mark.parametrize('attack', ['directory-bytes', 'false-count', 'zip64', 'multidisk'])
def test_eocd_preflight_rejects_before_zipfile_construction(tmp_path, signing, monkeypatch, attack):
    import struct
    archive = bundle(tmp_path, signing[0])
    raw = bytearray(archive.read_bytes())
    offset = raw.rfind(b'PK\x05\x06')
    if attack == 'directory-bytes': struct.pack_into('<I', raw, offset + 12, update.MAX_CENTRAL_DIRECTORY_BYTES + 1)
    elif attack == 'false-count':
        struct.pack_into('<HH', raw, offset + 8, 4, 4)
    elif attack == 'zip64': struct.pack_into('<HH', raw, offset + 8, 65535, 65535)
    else: struct.pack_into('<H', raw, offset + 4, 1)
    archive.write_bytes(raw)
    def forbidden(*args, **kwargs): raise AssertionError('ZipFile must not allocate entries')
    monkeypatch.setattr(update.zipfile, 'ZipFile', forbidden)
    with pytest.raises(update.UpdateError): update.verify(archive, signing[1])


def test_incoming_inplace_mutation_after_preflight_cannot_change_parsed_snapshot(tmp_path, signing, monkeypatch):
    archive = bundle(tmp_path, signing[0])
    original_preflight = update._preflight_zip
    original_zipfile = zipfile.ZipFile
    original_inode = archive.stat().st_ino
    observed_counts = []
    def mutate_source_after_snapshot_preflight(stream, size):
        original_preflight(stream, size)
        # Replace bytes in the SAME source inode after snapshot preflight.
        with original_zipfile(archive, 'w') as malicious:
            for index in range(update.MAX_FILES + 3):
                malicious.writestr(f'{index:04d}', b'')
        assert archive.stat().st_ino == original_inode
    def inspect_constructed_archive(stream, *args, **kwargs):
        parsed = original_zipfile(stream, *args, **kwargs)
        observed_counts.append(len(parsed.infolist()))
        return parsed
    monkeypatch.setattr(update, '_preflight_zip', mutate_source_after_snapshot_preflight)
    monkeypatch.setattr(update.zipfile, 'ZipFile', inspect_constructed_archive)
    result = update.verify(archive, signing[1])
    assert result['verified'] and result['file_count'] == 1
    assert observed_counts == [3]
    with original_zipfile(archive) as source:
        assert len(source.infolist()) == update.MAX_FILES + 3


def test_snapshot_source_identity_change_fails_before_zip_parser(tmp_path, signing, monkeypatch):
    archive = bundle(tmp_path, signing[0])
    original_fstat = update.os.fstat
    source_inode = archive.stat().st_ino
    calls = []
    def changed_metadata(descriptor):
        info = original_fstat(descriptor)
        if info.st_ino == source_inode:
            calls.append(1)
            if len(calls) == 2:
                from types import SimpleNamespace
                return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino,
                    st_size=info.st_size, st_mtime_ns=info.st_mtime_ns + 1,
                    st_ctime_ns=info.st_ctime_ns, st_mode=info.st_mode)
        return info
    def forbidden(*args, **kwargs): raise AssertionError('Changed source cannot reach ZIP allocation')
    monkeypatch.setattr(update.os, 'fstat', changed_metadata)
    monkeypatch.setattr(update.zipfile, 'ZipFile', forbidden)
    with pytest.raises(update.UpdateError, match='changed while snapshotting'):
        update.verify(archive, signing[1])
