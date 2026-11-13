"""HTTP integration proof with isolated galleries and no physical camera."""
import hashlib
import json
import threading
import time
from pathlib import Path

import pytest
from test_final_wave_product_api import product
from test_panorama import views


@pytest.mark.parametrize('method,path,body', [
    ('GET', '/device-health', None), ('POST', '/develop', {'image': 'x.jpg'}),
    ('GET', '/development/' + 'a' * 32 + '/linear.tiff', None),
    ('POST', '/api/camera/preview', {'camera_model': 'x', 'camera_port': 'usb:1,2'}),
    ('POST', '/api/camera/card/list', {'camera_model': 'x', 'camera_port': 'usb:1,2'}),
    ('POST', '/api/camera/card/import', {'camera_model': 'x', 'camera_port': 'usb:1,2', 'folder': '/DCIM', 'name': 'x.jpg'}),
    ('GET', '/api/camera/card/imports/' + 'card_' + ('a' * 32) + '/x.jpg', None),
    ('GET', '/sessions/' + 'a' * 32 + '/jobs', None),
    ('POST', '/sessions/' + 'a' * 32 + '/jobs', {}),
])
def test_new_private_routes_require_auth(product, method, path, body):
    response = product.client.request(method, path, **({'json': body} if body is not None else {}))
    assert response.status_code == 401


def test_preview_preserves_typed_failure_and_camera_lock(product, monkeypatch):
    from sonyremote import isolated_preview
    product.fake.name = 'real'
    monkeypatch.setattr(product.engine, 'RealEngine', lambda **kw: product.fake)
    calls = []
    def probe(model, port):
        assert product.app._camera_lock.locked()
        calls.append((model, port))
        raise isolated_preview.IsolatedProbeError('unsupported_memory_limit', 'host limit unavailable')
    monkeypatch.setattr(isolated_preview, 'capture_preview_isolated', probe)
    response = product.client.post('/api/camera/preview', headers=product.headers, json={
        'camera_model': sorted(product.engine.USB_CONTROL_MODELS)[0], 'camera_port': 'usb:000,003'})
    assert response.status_code == 503
    assert response.json()['detail']['code'] == 'unsupported_memory_limit'
    assert response.json()['detail']['retry_capture'] is False
    assert len(calls) == 1 and not product.app._camera_lock.locked()


def test_development_real_worker_download_delete_and_original_preserved(product):
    from PIL import Image
    Image.new('RGB', (80, 60), (64, 80, 96)).save(product.root / 'original.jpg')
    before = (product.root / 'original.jpg').read_bytes()
    response = product.client.post('/develop', headers=product.headers,
                                   json={'image': 'original.jpg', 'exposure_ev': .7, 'strength': .4})
    assert response.status_code == 200, response.text
    result = response.json()
    tiff = product.client.get(result['linear_tiff_url'], headers=product.headers)
    assert tiff.status_code == 200 and tiff.headers['content-type'] == 'image/tiff'
    assert hashlib.sha256(tiff.content).hexdigest() == result['evidence']['outputs']['linear.tiff']['sha256']
    assert (product.root / 'original.jpg').read_bytes() == before
    job = product.root / 'development' / result['evidence']['job_id']
    response = product.client.delete('/photos/' + result['image'], headers=product.headers)
    assert response.status_code == 200, response.text
    assert not job.exists()
    assert (product.root / 'original.jpg').read_bytes() == before


@pytest.mark.parametrize('failure', ['sidecar', 'enrollment'])
def test_development_publication_failure_rolls_back_only_copy(product, monkeypatch, failure):
    from PIL import Image
    import photo_lifecycle
    Image.new('RGB', (40, 30)).save(product.root / 'original.jpg')
    before = (product.root / 'original.jpg').read_bytes()
    def fail(*a, **kw):
        raise OSError('injected disk failure')
    if failure == 'sidecar':
        monkeypatch.setattr(product.engine, 'save_shot_record', fail)
    else:
        monkeypatch.setattr(photo_lifecycle, 'manage_shot', fail)
    response = product.client.post('/develop', headers=product.headers, json={'image': 'original.jpg'})
    assert response.status_code == 503, response.text
    assert response.json()['detail']['reason'] == 'publication_failed'
    assert list(product.root.glob('*.jpg')) == [product.root / 'original.jpg']
    assert not list((product.root / 'development').iterdir())
    assert (product.root / 'original.jpg').read_bytes() == before


def test_job_submit_returns_202_and_get_does_not_reexecute(product, monkeypatch):
    import product_sessions
    from stack_session import SessionRunner
    runner = SessionRunner(product.root / 'sessions')
    state = runner.create('timelapse', 2, metadata={'camera_model': 'Sony ILCE-7M2 (Control)',
                                                  'camera_port': 'usb:000,003',
                                                  'camera_serial': 'A7II00SERIAL'})
    entered, release = threading.Event(), threading.Event()
    calls = []
    def run(sid):
        calls.append(sid)
        entered.set()
        assert release.wait(3)
        value = runner.read(sid)
        value['status'] = 'cancelled'
        return value
    monkeypatch.setattr(product_sessions, 'runner', lambda: runner)
    monkeypatch.setattr(product_sessions, 'run', run)
    product.fake.name = 'real'
    try:
        path = '/sessions/' + state['id'] + '/jobs'
        assert product.client.get(path, headers=product.headers).json()['status'] == 'not_submitted'
        response = product.client.post(path, headers=product.headers, json={})
        assert response.status_code == 202
        assert entered.wait(1)
        assert product.client.post(path, headers=product.headers, json={}).status_code == 202
        assert product.client.get(path, headers=product.headers).json()['status'] == 'running'
        assert len(calls) == 1
    finally:
        release.set()


def test_gallery_cannot_delete_active_owned_session(product):
    from PIL import Image
    from photo_lifecycle import PhotoLifecycle
    from stack_session import SessionRunner, _atomic_json
    runner = SessionRunner(product.root / 'sessions')
    state = runner.create('hdr', 3)
    Image.new('RGB', (8, 8)).save(product.root / 'stack.jpg')
    path = runner._directory(state['id'])
    _atomic_json(path / 'job.json', {'id': state['id'], 'status': 'queued'})
    photos = PhotoLifecycle(product.root)
    photos.enroll('stack.jpg', [f'sessions/{state["id"]}/manifest.json', f'sessions/{state["id"]}/job.json'])
    with pytest.raises(ValueError, match='active'):
        photos.delete('stack.jpg')
    assert (product.root / 'stack.jpg').exists() and (path / 'manifest.json').exists()
    _atomic_json(path / 'job.json', {'id': state['id'], 'status': 'completed'})
    assert photos.delete('stack.jpg')['deleted_count'] > 0


def test_verified_development_download_survives_gallery_delete(product):
    import asyncio
    from PIL import Image
    from photo_lifecycle import PhotoLifecycle
    Image.new('RGB', (40, 30)).save(product.root / 'original.jpg')
    result = product.client.post('/develop', headers=product.headers,
                                 json={'image': 'original.jpg'}).json()
    response = product.app.development_artifact(result['evidence']['job_id'], 'linear.tiff')
    PhotoLifecycle(product.root).delete(result['image'])
    async def consume():
        return b''.join([chunk async for chunk in response.body_iterator])
    payload = asyncio.run(consume())
    assert hashlib.sha256(payload).hexdigest() == result['evidence']['outputs']['linear.tiff']['sha256']


def test_panorama_plan_auth_and_invalid_sources_do_not_capture(product):
    response = product.client.post('/panoramas/plan', headers=product.headers,
                                   json={'rows': 2, 'columns': 3})
    assert response.status_code == 200
    assert len(response.json()['frames']) == 6
    assert not response.json()['capture_performed']
    optics = product.client.post('/panoramas/plan', headers=product.headers, json={
        'focal_length_mm': 24, 'sensor_width_mm': 35.8, 'sensor_height_mm': 23.9,
        'yaw_span_deg': 120, 'pitch_span_deg': 80, 'overlap': .35})
    assert optics.status_code == 200, optics.text
    body = optics.json()
    assert body['rows'] == 2 and body['columns'] == 2
    assert body['geometry'] == 'pinhole_atan'
    assert body['session']['frame_count'] == 4
    assert product.client.post('/panoramas', json={'images': ['a.jpg', 'b.jpg']}).status_code == 401
    assert product.client.post('/panoramas', headers=product.headers,
        json={'images': ['../private.jpg', 'b.jpg']}).status_code == 400
    assert not product.calls


def test_panorama_worker_publishes_and_enrolls_full_job(product, views):
    import shutil
    sources, _, _ = views
    for path in sources:
        shutil.copyfile(path, product.root / path.name)
    before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    response = product.client.post('/panoramas', headers=product.headers,
        json={'images': [path.name for path in sources], 'projection': 'rectilinear'})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['ok'] and result['evidence']['status'] == 'completed'
    output = result['evidence']['output']
    horizon = result['evidence']['horizon']
    assert output['width'] >= 900
    assert output['pre_crop_width'] >= output['post_crop_width'] == output['width']
    assert output['pre_crop_height'] >= output['post_crop_height'] == output['height']
    assert 'leveling_angle_deg' in horizon
    assert horizon['applied_deg'] == horizon['leveling_angle_deg']
    assert result['evidence']['output']['sha256'] == hashlib.sha256((product.root / result['image']).read_bytes()).hexdigest()
    job = product.root / 'panoramas' / result['evidence']['job_id']
    assert product.client.delete('/photos/' + result['image'], headers=product.headers).status_code == 200
    assert not job.exists()
    assert all(hashlib.sha256((product.root / name).read_bytes()).hexdigest() == digest for name, digest in before.items())


def test_running_preview_download_survives_rotation(product, monkeypatch):
    import asyncio
    import product_sessions
    from stack_session import SessionRunner, binary_receipt
    runner = SessionRunner(product.root / 'sessions')
    state = runner.create('timelapse', 2)
    path = runner._directory(state['id']) / 'preview_0002.mp4'
    payload = b'fixture bytes: descriptor lifetime only; not codec proof'
    path.write_bytes(payload)
    state['preview_video'] = {'status': 'completed', 'name': path.name, 'receipt': binary_receipt(path)}
    runner.save(state)
    monkeypatch.setattr(product_sessions, 'runner', lambda: runner)
    response = product.app.session_artifact(state['id'], path.name)
    path.unlink()
    async def consume():
        return b''.join([chunk async for chunk in response.body_iterator])
    assert asyncio.run(consume()) == payload


def test_mock_job_view_does_not_acquire_live_scheduler(product, monkeypatch):
    import product_sessions
    from stack_session import SessionRunner
    runner = SessionRunner(product.root / 'sessions')
    state = runner.create('timelapse', 2)
    monkeypatch.setattr(product_sessions, 'runner', lambda: runner)
    def forbidden():
        pytest.fail('Demo read must not acquire the live scheduler lease')
    monkeypatch.setattr(product.app, '_session_jobs', forbidden)
    path = '/sessions/' + state['id']
    assert product.client.get(path + '/jobs', headers=product.headers).json()['status'] == 'not_submitted'
    assert product.client.post(path + '/cancel', headers=product.headers, json={}).status_code == 400
    assert product.client.post(path + '/resume', headers=product.headers, json={}).status_code == 400


def test_dng_failure_preserves_original_and_cleans_derived_job(product, monkeypatch):
    from PIL import Image
    import linear_dng
    Image.new('RGB', (40, 30), (60, 80, 90)).save(product.root/'original.jpg')
    original = (product.root/'original.jpg').read_bytes()
    def fail(*args, **kwargs): raise ValueError('decoder unavailable')
    monkeypatch.setattr(linear_dng, 'export_linear_dng', fail)
    response = product.client.post('/develop', headers=product.headers,
        json={'image':'original.jpg','include_dng':True})
    assert response.status_code == 503
    assert (product.root/'original.jpg').read_bytes() == original
    assert not list((product.root/'development').iterdir())
    assert list(product.root.glob('*.jpg')) == [product.root/'original.jpg']


def test_dng_actual_independent_decoder_download_and_lifecycle(product):
    pytest.importorskip('rawpy')
    from PIL import Image
    import hashlib
    Image.new('RGB',(80,60),(60,90,120)).save(product.root/'original.jpg')
    response = product.client.post('/develop', headers=product.headers,
        json={'image':'original.jpg','include_dng':True})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['evidence']['linear_dng']['verification']['status'] == 'verified'
    assert result['evidence']['linear_dng']['sensor_raw'] is False
    assert result['evidence']['linear_dng_url'] == result['linear_dng_url']
    download = product.client.get(result['linear_dng_url'],headers=product.headers)
    assert download.status_code == 200
    assert download.headers['content-type'] == 'image/x-adobe-dng'
    assert hashlib.sha256(download.content).hexdigest() == result['evidence']['outputs']['output.dng']['sha256']
    deleted = product.client.delete('/photos/'+result['image'],headers=product.headers)
    assert deleted.status_code == 200
    assert product.client.get(result['linear_dng_url'],headers=product.headers).status_code == 404
    assert (product.root/'original.jpg').exists()


def test_radio_candidate_plan_is_authenticated_and_not_live_state(product):
    assert product.client.post('/device/radio-plan',json={'band':'5ghz','channel':36}).status_code == 401
    response = product.client.post('/device/radio-plan',headers=product.headers,json={'band':'5ghz','channel':36})
    assert response.status_code == 200
    value = response.json()
    assert value['host_changes'] is False and value['access_point_started'] is False
    assert value['configuration']['hardware_support_verified'] is False
    assert product.client.post('/device/radio-plan',headers=product.headers,json={'band':'5ghz','channel':6}).status_code == 422
