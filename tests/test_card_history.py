from pathlib import Path
import hashlib
import os
import sys
import threading

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import card_history as card


class ReadOnlyCard:
    def __init__(self):
        self.calls = []
        self.data = b'original camera file'

    def folders(self, folder):
        self.calls.append(('folders', folder))
        return ['DCIM'] if folder == '/' else []

    def files(self, folder):
        self.calls.append(('files', folder))
        return ['IMG001.JPG'] if folder == '/DCIM' else []

    def info(self, folder, name):
        self.calls.append(('info', folder, name))
        return {'bytes': len(self.data), 'mime_type': 'image/jpeg', 'mtime': 42}

    def download(self, folder, name, destination):
        self.calls.append(('download', folder, name))
        with os.fdopen(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as handle:
            handle.write(self.data)


def test_lists_readonly_with_explicit_metadata_grade():
    adapter = ReadOnlyCard()
    result = card.list_from_adapter(adapter)
    assert result['folders'] == ['/', '/DCIM']
    assert result['files'][0]['downloadable']
    assert result['file_count'] == 1 and result['read_only']
    assert {row[0] for row in adapter.calls} == {'folders', 'files', 'info'}


def test_selected_download_hash_and_no_delete_or_capture(tmp_path):
    adapter = ReadOnlyCard()
    destination = tmp_path / 'copy.JPG'
    result = card.download_from_adapter(adapter, '/DCIM', 'IMG001.JPG', destination)
    assert result['sha256'] == hashlib.sha256(adapter.data).hexdigest()
    assert result['card_deleted'] is False and result['retry_capture'] is False
    assert destination.read_bytes() == adapter.data
    assert {row[0] for row in adapter.calls} == {'files', 'info', 'download'}


@pytest.mark.parametrize('folder,name', [('/../escape','x.jpg'), ('/DCIM/','x.jpg'), ('//DCIM','x.jpg'), ('/DCIM','../x.jpg'), ('/DCIM','x\\x.jpg'), ('/DCIM','x\n.jpg'), ('relative','x.jpg')])
def test_rejects_paths_before_adapter_call(tmp_path, folder, name):
    adapter = ReadOnlyCard()
    with pytest.raises(card.CardHistoryError):
        card.download_from_adapter(adapter, folder, name, tmp_path / 'out')
    assert not adapter.calls


def test_download_requires_membership_and_size(tmp_path):
    adapter = ReadOnlyCard()
    with pytest.raises(card.CardHistoryError) as raised:
        card.download_from_adapter(adapter, '/DCIM', 'OTHER.JPG', tmp_path / 'out')
    assert raised.value.reason == 'not_found'
    adapter.info = lambda *args: {'bytes': None}
    with pytest.raises(card.CardHistoryError) as raised:
        card.download_from_adapter(adapter, '/DCIM', 'IMG001.JPG', tmp_path / 'out')
    assert raised.value.reason == 'limit'
    assert not any(row[0] == 'download' for row in adapter.calls)


def test_changed_card_metadata_withholds_success(tmp_path):
    adapter = ReadOnlyCard()
    count = 0
    def changing_info(*args):
        nonlocal count
        count += 1
        return {'bytes': len(adapter.data), 'mtime': count}
    adapter.info = changing_info
    with pytest.raises(card.CardHistoryError) as raised:
        card.download_from_adapter(adapter, '/DCIM', 'IMG001.JPG', tmp_path / 'out')
    assert raised.value.reason == 'source_changed'


def test_folder_and_file_budgets(monkeypatch):
    adapter = ReadOnlyCard()
    monkeypatch.setattr(card, 'MAX_FOLDERS', 1)
    with pytest.raises(card.CardHistoryError) as raised:
        card.list_from_adapter(adapter)
    assert raised.value.reason == 'limit'
    monkeypatch.setattr(card, 'MAX_FOLDERS', 128)
    monkeypatch.setattr(card, 'MAX_FILES', 0)
    with pytest.raises(card.CardHistoryError) as raised:
        card.list_from_adapter(adapter)
    assert raised.value.reason == 'limit'


def test_exact_identity_required_before_process(monkeypatch):
    monkeypatch.setattr(card.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not launch'))
    with pytest.raises(card.CardHistoryError):
        card.list_card('Sony', 'usb:001,002')
    with pytest.raises(card.CardHistoryError):
        card.list_card('Sony ILCE-7M2 (Control)', '')
    from sonyremote.camera import CameraIdentityError
    with pytest.raises(CameraIdentityError) as raised:
        card.list_card('Sony ILCE-7M2 (Control)', 'usb:001,002')
    assert raised.value.reason == 'serial_required'


def _matching_serial(monkeypatch, serial='A7II00SERIAL'):
    monkeypatch.setattr('sonyremote.camera.verify_serial_at_port',
                        lambda model, port, expected: {'serial': expected or serial, 'port': port, 'model': model})


def test_supervisor_timeout_kills_isolated_child_without_camera(monkeypatch):
    class Child:
        pid = 987654
        returncode = None
        def poll(self): return None
        def wait(self, timeout): self.returncode = -9
    child = Child()
    killed = []
    _matching_serial(monkeypatch)
    monkeypatch.setattr(card.subprocess, 'Popen', lambda *a, **k: child)
    monkeypatch.setattr(card.os, 'killpg', lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(card, 'MAX_SECONDS', -1)
    with pytest.raises(card.CardHistoryError) as raised:
        card.list_card('Sony ILCE-7M2 (Control)', 'usb:001,002', serial='A7II00SERIAL')
    assert raised.value.reason == 'timeout'
    assert killed == [child.pid]


def test_fd_adapter_never_calls_capture_or_write_settings(tmp_path):
    import gphoto2 as gp
    class ReadOnlyGPhoto:
        def file_get(self, folder, name, kind, camera_file):
            assert folder == '/DCIM' and name == 'IMG001.JPG'
            assert kind == gp.GP_FILE_TYPE_NORMAL
            assert isinstance(camera_file, gp.CameraFile)
    # Actual installed gphoto fd construction/destruction, but no real camera object.
    card.GPhotoCardAdapter(ReadOnlyGPhoto(), gp).download('/DCIM', 'IMG001.JPG', tmp_path / 'copy.jpg')
    assert (tmp_path / 'copy.jpg').stat().st_mode & 0o777 == 0o600


def test_precanceled_request_does_not_open_camera(monkeypatch):
    cancel = threading.Event()
    cancel.set()
    _matching_serial(monkeypatch)
    monkeypatch.setattr(card.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not open camera'))
    with pytest.raises(card.CardHistoryError) as raised:
        card.list_card('Sony ILCE-7M2 (Control)', 'usb:001,002', serial='A7II00SERIAL', cancel_event=cancel)
    assert raised.value.reason == 'canceled'


def test_mismatched_serial_refuses_before_import_directory(monkeypatch, tmp_path):
    from sonyremote.camera import CameraIdentityError
    monkeypatch.setattr(card.subprocess, 'Popen', lambda *a, **k: pytest.fail('must not launch'))

    def mismatch(model, port, serial):
        raise CameraIdentityError('swap', reason='serial_mismatch', expected_serial=serial,
                                  observed_serial='AAAA1111BBBB', model=model, port=port)

    monkeypatch.setattr('sonyremote.camera.verify_serial_at_port', mismatch)
    destination = tmp_path / 'card-imports'
    with pytest.raises(CameraIdentityError) as raised:
        card.download_card_file('Sony ILCE-7M2 (Control)', 'usb:001,002', '/DCIM', 'IMG001.JPG', destination,
                                serial='A7II00SERIAL')
    assert raised.value.reason == 'serial_mismatch'
    assert not destination.exists() or not any(destination.rglob('*'))
