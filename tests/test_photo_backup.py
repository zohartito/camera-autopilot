from pathlib import Path
import hashlib
import json
import sys
import threading

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import photo_backup as backup


def setup(tmp_path):
    source, destination = tmp_path / 'gallery', tmp_path / 'destination'
    source.mkdir()
    destination.mkdir()
    (source / 'one.jpg').write_bytes(b'photo one')
    (source / 'two.jpg').write_bytes(b'photo two')
    return source, destination


def test_verified_copy_and_idempotent_resume_never_claims_independent_media(tmp_path):
    source, destination = setup(tmp_path)
    first = backup.backup_gallery(source, destination)
    second = backup.backup_gallery(source, destination)
    assert first['backup_id'] == second['backup_id']
    assert all(row['reused'] for row in second['files'])
    assert second['receipt_reused']
    assert first['file_count'] == 2
    assert not first['different_logical_filesystem']
    assert not first['independent_failure_protection_verified']
    assert first['physical_media_independence'] == 'unverified'
    assert not first['original_deleted']
    for row in first['files']:
        copied = destination / 'camera-autopilot-backup' / row['copy']
        assert copied.read_bytes() == (source / row['source_image']).read_bytes()
        assert hashlib.sha256(copied.read_bytes()).hexdigest() == row['sha256']
        assert copied.stat().st_mode & 0o777 == 0o600
    assert (destination / first['receipt']).is_file()
    assert not list(destination.rglob('.partial-*'))


def test_explicit_selection_excludes_other_files(tmp_path):
    source, destination = setup(tmp_path)
    result = backup.backup_gallery(source, destination, ['one.jpg'])
    assert [row['source_image'] for row in result['files']] == ['one.jpg']


@pytest.mark.parametrize('names', [['../one.jpg'], ['one.jpg', 'one.jpg'], ['unknown.txt'], 'one.jpg', [], [{}]])
def test_bad_selection(tmp_path, names):
    source, destination = setup(tmp_path)
    with pytest.raises(backup.BackupError):
        backup.backup_gallery(source, destination, names)


def test_source_and_destination_symlinks_refused(tmp_path):
    source, destination = setup(tmp_path)
    (source / 'link.jpg').symlink_to(source / 'one.jpg')
    with pytest.raises(OSError):
        backup.backup_gallery(source, destination, ['link.jpg'])
    alias = tmp_path / 'alias'
    alias.symlink_to(destination)
    with pytest.raises(backup.BackupError):
        backup.backup_gallery(source, alias)


def test_destination_inside_source_refused(tmp_path):
    source, _ = setup(tmp_path)
    nested = source / 'backup'
    nested.mkdir()
    with pytest.raises(backup.BackupError):
        backup.backup_gallery(source, nested)


def test_corrupt_existing_copy_detected_and_not_overwritten(tmp_path):
    source, destination = setup(tmp_path)
    result = backup.backup_gallery(source, destination)
    copy = destination / 'camera-autopilot-backup' / result['files'][0]['copy']
    copy.write_bytes(b'corrupt')
    with pytest.raises(backup.BackupError) as raised:
        backup.backup_gallery(source, destination)
    assert raised.value.reason == 'verification_failed'
    assert copy.read_bytes() == b'corrupt'
    assert (source / 'one.jpg').read_bytes() == b'photo one'


def test_cancellation_cleans_temporary_and_preserves_sources(tmp_path, monkeypatch):
    source, destination = setup(tmp_path)
    cancel = threading.Event()
    real_hash = backup._hash
    def stop(path, started, event):
        cancel.set()
        return real_hash(path, started, event)
    monkeypatch.setattr(backup, '_hash', stop)
    with pytest.raises(backup.BackupError) as raised:
        backup.backup_gallery(source, destination, cancel_event=cancel)
    assert raised.value.reason == 'canceled'
    assert not list(destination.rglob('.partial-*'))
    assert len(list(source.iterdir())) == 2


def test_byte_and_time_budgets(tmp_path, monkeypatch):
    source, destination = setup(tmp_path)
    monkeypatch.setattr(backup, 'MAX_TOTAL_BYTES', 2)
    with pytest.raises(backup.BackupError) as raised:
        backup.backup_gallery(source, destination)
    assert raised.value.reason == 'limit'
    monkeypatch.setattr(backup, 'MAX_SECONDS', -1)
    with pytest.raises(backup.BackupError) as raised:
        backup.backup_gallery(source, destination)
    assert raised.value.reason == 'timeout'


def test_tampered_receipt_is_not_reused(tmp_path):
    source, destination = setup(tmp_path)
    result = backup.backup_gallery(source, destination)
    receipt = destination / result['receipt']
    data = json.loads(receipt.read_text())
    data['files'][0]['sha256'] = 'false'
    receipt.write_text(json.dumps(data))
    with pytest.raises(backup.BackupError) as raised:
        backup.backup_gallery(source, destination)
    assert raised.value.reason == 'verification_failed'


def test_source_hardlink_is_not_counted_as_second_copy(tmp_path):
    import os
    source, destination = setup(tmp_path)
    result = backup.backup_gallery(source, destination, ['one.jpg'])
    copied = destination / 'camera-autopilot-backup' / result['files'][0]['copy']
    copied.unlink()
    os.link(source / 'one.jpg', copied)
    with pytest.raises(backup.BackupError) as raised:
        backup.backup_gallery(source, destination, ['one.jpg'])
    assert raised.value.reason == 'verification_failed'


@pytest.mark.parametrize('target', ['source', 'existing_copy'])
def test_fifo_source_or_copy_rejected_without_blocking(tmp_path, target):
    import os
    import subprocess
    source, destination = setup(tmp_path)
    if target == 'source':
        fifo = source / 'one.jpg'
    else:
        receipt = backup.backup_gallery(source, destination, ['one.jpg'])
        fifo = destination / 'camera-autopilot-backup' / receipt['files'][0]['copy']
    fifo.unlink()
    os.mkfifo(fifo)
    script = '''from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from photo_backup import backup_gallery, BackupError
try:
    backup_gallery(Path(sys.argv[2]), Path(sys.argv[3]), ['one.jpg'])
except BackupError:
    raise SystemExit(0)
raise SystemExit(2)
'''
    result = subprocess.run([sys.executable, '-c', script, str(Path(backup.__file__).parent), str(source), str(destination)], timeout=3, capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
