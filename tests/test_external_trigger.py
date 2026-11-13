from pathlib import Path
import hashlib
import os
import subprocess
import sys

import pytest
from PIL import Image
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
import external_trigger as external

CAMERA = {'model': 'Sony ILCE-7M2 (Control)', 'port': 'usb:000,003'}
EXPOSURE = {'iso': 100, 'aperture': 5.6, 'shutter': '1/60'}


def photo(tmp_path, name='one.jpg', color=(100, 100, 100), model='ILCE-7M2', exif_present=True):
    path = tmp_path / name
    exif = Image.Exif()
    if exif_present:
        exif[272] = model
        exif[34855] = 100
        exif[33437] = 5.6
        exif[33434] = 1 / 60
    Image.new('RGB', (80, 60), color).save(path, exif=exif, quality=100)
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def manager(tmp_path, frames=1):
    runner = external.ExternalTriggerIngest(tmp_path.resolve() / 'ingest')
    state = runner.create(**CAMERA, frame_count=frames, settle_seconds=.05)
    return runner, state['id']


def supply(runner, session, source, digest, event='e0', sequence=0):
    runner.observe_event(session, event, sequence, source.name, digest, CAMERA)
    return runner.supply_file(session, event, source, CAMERA, EXPOSURE)


def accept(runner, session, event, monkeypatch):
    state = runner.read(session)['events'][event]
    monkeypatch.setattr(external.time, 'time', lambda: state['stable_since'] + .1)
    return runner.observe_exposure(session, event, CAMERA, EXPOSURE)


def test_durable_event_file_settle_acceptance_and_source_preserved(tmp_path, monkeypatch):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    before = source.read_bytes()
    event = supply(runner, session, source, digest)
    assert event['status'] == 'awaiting_settle'
    with pytest.raises(external.ExternalTriggerError) as raised:
        runner.render_inputs(session)
    assert raised.value.reason == 'incomplete'
    accepted = accept(runner, session, 'e0', monkeypatch)
    assert accepted['status'] == 'accepted'
    restarted = external.ExternalTriggerIngest(runner.root)
    frames = restarted.render_inputs(session)
    assert frames[0]['receipt']['sha256'] == digest
    assert frames[0]['applied']['native_route_verified'] is False
    assert restarted.read(session)['status'] == 'completed'
    assert source.read_bytes() == before
    assert Path(frames[0]['receipt']['path']).stat().st_mode & 0o777 == 0o600
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in (runner.root / session).iterdir())


def test_duplicate_event_and_file_do_not_reset_settle_or_add_frames(tmp_path):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    first = supply(runner, session, source, digest)
    duplicate = runner.observe_event(session, 'e0', 0, source.name, digest, CAMERA)
    second = runner.supply_file(session, 'e0', source, CAMERA, EXPOSURE)
    assert duplicate['duplicate'] and second['duplicate']
    assert second['stable_since'] == first['stable_since']
    assert len(runner.read(session)['events']) == 1


def test_conflicting_duplicate_and_sequence_refused(tmp_path):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    supply(runner, session, source, digest)
    for event, sha in [('e0', '0' * 64), ('other', digest)]:
        with pytest.raises(external.ExternalTriggerError) as raised:
            runner.observe_event(session, event, 0, source.name, sha, CAMERA)
        assert raised.value.reason == 'event_conflict'


def test_late_event_preserves_sequence_order_for_render(tmp_path, monkeypatch):
    runner, session = manager(tmp_path, 2)
    second, second_hash = photo(tmp_path, 'second.jpg', color=(140, 140, 140))
    first, first_hash = photo(tmp_path, 'first.jpg')
    supply(runner, session, second, second_hash, 'e1', 1)
    accept(runner, session, 'e1', monkeypatch)
    late = supply(runner, session, first, first_hash, 'e0', 0)
    assert late['late']
    accept(runner, session, 'e0', monkeypatch)
    frames = runner.render_inputs(session)
    assert [row['event_id'] for row in frames] == ['e0', 'e1']
    from stack_session import merge_session_frames, frame_receipt
    output = tmp_path / 'merged.jpg'
    merge_session_frames('night', [row['receipt']['path'] for row in frames], str(output))
    assert frame_receipt(output)['width'] == 80


def test_exposure_change_resets_settle_and_cannot_accept_wrong_metadata(tmp_path, monkeypatch):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    first = supply(runner, session, source, digest)
    monkeypatch.setattr(external.time, 'time', lambda: first['stable_since'] + 1)
    wrong = dict(EXPOSURE, iso=200)
    state = runner.observe_exposure(session, 'e0', CAMERA, wrong)
    assert state['status'] == 'awaiting_settle' and not state['exposure_matches_image']
    state = runner.observe_exposure(session, 'e0', CAMERA, EXPOSURE)
    assert state['observations'] == 1 and state['status'] == 'awaiting_settle'
    monkeypatch.setattr(external.time, 'time', lambda: first['stable_since'] + 2)
    assert runner.observe_exposure(session, 'e0', CAMERA, EXPOSURE)['status'] == 'accepted'


def test_repeated_observation_without_elapsed_settle_is_not_acceptance(tmp_path, monkeypatch):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    first = supply(runner, session, source, digest)
    monkeypatch.setattr(external.time, 'time', lambda: first['stable_since'])
    for _ in range(3):
        assert runner.observe_exposure(session, 'e0', CAMERA, EXPOSURE)['status'] == 'awaiting_settle'


def test_restart_recovers_published_file_before_manifest_completion(tmp_path, monkeypatch):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    runner.observe_event(session, 'e0', 0, source.name, digest, CAMERA)
    original_save = runner._save
    def fail_after_copy(directory, state):
        if state['events']['e0']['status'] == 'awaiting_settle':
            raise OSError('interruption after file publication')
        return original_save(directory, state)
    monkeypatch.setattr(runner, '_save', fail_after_copy)
    with pytest.raises(OSError):
        runner.supply_file(session, 'e0', source, CAMERA, EXPOSURE)
    source.unlink()  # Recover from the exact private copy, without source or recapture.
    restarted = external.ExternalTriggerIngest(runner.root)
    recovered = restarted.supply_file(session, 'e0', source, CAMERA, EXPOSURE)
    assert recovered['status'] == 'awaiting_settle' and recovered['file']['sha256'] == digest
    accept(restarted, session, 'e0', monkeypatch)
    assert restarted.render_inputs(session)[0]['receipt']['sha256'] == digest


@pytest.mark.parametrize('problem', ['hash', 'model', 'exif', 'exposure'])
def test_unverified_file_cannot_be_accepted(tmp_path, problem):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path, model='ILCE-7M3' if problem == 'model' else 'ILCE-7M2', exif_present=problem != 'exif')
    runner.observe_event(session, 'e0', 0, source.name, '0' * 64 if problem == 'hash' else digest, CAMERA)
    with pytest.raises(external.ExternalTriggerError):
        runner.supply_file(session, 'e0', source, CAMERA, dict(EXPOSURE, iso=200) if problem == 'exposure' else EXPOSURE)
    state = runner.read(session)['events']['e0']
    assert state['status'] != 'accepted'
    assert state['last_error']['retry_capture'] is False
    assert source.exists()
    assert not list((runner.root / session).glob('*.jpg'))


def test_wrong_camera_refused_before_ingress(tmp_path):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    other = dict(CAMERA, port='usb:000,004')
    with pytest.raises(external.ExternalTriggerError) as raised:
        runner.observe_event(session, 'e0', 0, source.name, digest, other)
    assert raised.value.reason == 'camera_mismatch'
    assert not runner.read(session)['events']


def test_native_route_is_gated(tmp_path):
    runner = external.ExternalTriggerIngest(tmp_path.resolve() / 'ingest')
    with pytest.raises(external.ExternalTriggerError) as raised:
        runner.create(**CAMERA, frame_count=2, adapter='native')
    assert raised.value.reason == 'native_route_unverified'
    assert not any(path.is_dir() for path in runner.root.iterdir())


def test_deleted_accepted_copy_refuses_render_or_duplicate_acceptance(tmp_path, monkeypatch):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    event = supply(runner, session, source, digest)
    accept(runner, session, 'e0', monkeypatch)
    Path(event['file']['path']).unlink()
    with pytest.raises(external.ExternalTriggerError) as raised:
        runner.render_inputs(session)
    assert raised.value.reason == 'missing_file'
    with pytest.raises(external.ExternalTriggerError):
        runner.supply_file(session, 'e0', source, CAMERA, EXPOSURE)
    assert source.exists()


def test_expired_event_never_requests_replacement(tmp_path, monkeypatch):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    event = runner.observe_event(session, 'e0', 0, source.name, digest, CAMERA)
    monkeypatch.setattr(external.time, 'time', lambda: event['observed_at'] + 3601)
    with pytest.raises(external.ExternalTriggerError) as raised:
        runner.supply_file(session, 'e0', source, CAMERA, EXPOSURE)
    assert raised.value.reason == 'expired' and raised.value.retry_capture is False


def test_fifo_source_rejected_before_blocking(tmp_path):
    runner, session = manager(tmp_path)
    fifo = tmp_path / 'fifo.jpg'
    os.mkfifo(fifo)
    runner.observe_event(session, 'e0', 0, fifo.name, '0' * 64, CAMERA)
    script = '''import json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from external_trigger import ExternalTriggerIngest,ExternalTriggerError
try:
    ExternalTriggerIngest(sys.argv[2]).supply_file(sys.argv[3],'e0',Path(sys.argv[4]),json.loads(sys.argv[5]),json.loads(sys.argv[6]))
except ExternalTriggerError:
    raise SystemExit(0)
raise SystemExit(2)
'''
    import json
    process = subprocess.run([sys.executable, '-c', script, str(Path(external.__file__).parent), str(runner.root), session, str(fifo), json.dumps(CAMERA), json.dumps(EXPOSURE)], timeout=3, capture_output=True)
    assert process.returncode == 0, process.stderr.decode()


def test_ingress_limits_and_symlinks(tmp_path, monkeypatch):
    runner, session = manager(tmp_path)
    source, digest = photo(tmp_path)
    runner.observe_event(session, 'e0', 0, source.name, digest, CAMERA)
    monkeypatch.setattr(external, 'MAX_FRAME_BYTES', 2)
    with pytest.raises(external.ExternalTriggerError) as raised:
        runner.supply_file(session, 'e0', source, CAMERA, EXPOSURE)
    assert raised.value.reason == 'limit'
    monkeypatch.setattr(external, 'MAX_FRAME_BYTES', 64 * 1024 * 1024)
    renamed = source.with_name('original.jpg')
    source.rename(renamed)
    source.symlink_to(renamed)
    with pytest.raises(OSError):
        runner.supply_file(session, 'e0', source, CAMERA, EXPOSURE)


def test_concurrent_session_operation_refuses_without_waiting(tmp_path):
    runner, session = manager(tmp_path)
    with runner._locked(session):
        with pytest.raises(external.ExternalTriggerError) as raised:
            runner.read(session)
    assert raised.value.reason == 'busy'


def test_one_file_cannot_masquerade_as_two_external_exposures(tmp_path):
    runner, session = manager(tmp_path, 2)
    source, digest = photo(tmp_path)
    runner.observe_event(session, 'e0', 0, source.name, digest, CAMERA)
    with pytest.raises(external.ExternalTriggerError) as raised:
        runner.observe_event(session, 'e1', 1, 'renamed.jpg', digest, CAMERA)
    assert raised.value.reason == 'duplicate_file'
    assert len(runner.read(session)['events']) == 1


def test_interrupted_publication_still_reserves_ingress_budget(tmp_path, monkeypatch):
    runner, session = manager(tmp_path, 2)
    first, first_hash = photo(tmp_path, 'first.jpg')
    second, second_hash = photo(tmp_path, 'second.jpg', color=(140, 140, 140))
    monkeypatch.setattr(external, 'MAX_TOTAL_BYTES', first.stat().st_size + second.stat().st_size - 1)
    runner.observe_event(session, 'e0', 0, first.name, first_hash, CAMERA)
    save = runner._save
    def crash_after_file(directory, state):
        if state['events']['e0']['status'] == 'awaiting_settle':
            raise OSError('interrupted before receipt state publication')
        return save(directory, state)
    monkeypatch.setattr(runner, '_save', crash_after_file)
    with pytest.raises(OSError):
        runner.supply_file(session, 'e0', first, CAMERA, EXPOSURE)
    restarted = external.ExternalTriggerIngest(runner.root)
    interrupted = restarted.read(session)['events']['e0']
    assert interrupted['file'] is None and interrupted['reserved_bytes'] == first.stat().st_size
    restarted.observe_event(session, 'e1', 1, second.name, second_hash, CAMERA)
    with pytest.raises(external.ExternalTriggerError) as raised:
        restarted.supply_file(session, 'e1', second, CAMERA, EXPOSURE)
    assert raised.value.reason == 'limit'
    assert not (runner.root / session / 'frame_0001.jpg').exists()
    assert first.exists() and second.exists()
