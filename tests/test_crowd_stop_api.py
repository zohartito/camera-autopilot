"""Crowd Control routes: the auto-stop request field, POST /sessions/{id}/stop and the
hash-verified /sessions/{id}/crowd-preview stream. Camera-free (``product`` fixture)."""
import hashlib
import importlib

from test_final_wave_product_api import product  # noqa: F401  (pytest fixture import)
from test_section1_api import _jpeg, _real_runner


def _body(product, **extra):
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    return {'mode': 'crowd_remove', 'frames': 8, 'camera_model': model, 'camera_port': 'usb:000,003',
            'camera_serial': 'A7III0SERIAL', **extra}


def test_create_folds_stop_when_cleared_for_crowd_only(product):
    _real_runner(product)
    created = product.client.post('/sessions', headers=product.headers, json=_body(product, stop_when_cleared=0.95))
    assert created.status_code == 200, created.text
    assert created.json()['metadata']['stop_when_cleared'] == 0.95
    plain = product.client.post('/sessions', headers=product.headers, json=_body(product))
    assert 'stop_when_cleared' not in plain.json()['metadata']
    for bad in (_body(product, mode='hdr', frames=3, stop_when_cleared=0.9),
                _body(product, frames=5, stop_when_cleared=0.9),     # cannot stop before frame 5
                _body(product, frames=9, stop_when_cleared=0.9),     # no previews past frame 8
                _body(product, frames=16, stop_when_cleared=0.9),
                _body(product, stop_when_cleared=0.3),
                _body(product, stop_when_cleared=1.5)):
        response = product.client.post('/sessions', headers=product.headers, json=bad)
        assert response.status_code in (400, 422), response.text


def test_auto_stop_beyond_the_estimator_limit_is_refused_with_a_reason(product):
    _real_runner(product)
    response = product.client.post('/sessions', headers=product.headers, json=_body(product, frames=12, stop_when_cleared=0.9))
    assert response.status_code == 400
    assert 'at most 8 frames' in response.json()['detail']
    # Without the threshold a 12-frame crowd session (manual Stop) is still accepted.
    assert product.client.post('/sessions', headers=product.headers, json=_body(product, frames=12)).status_code == 200


def test_stop_route_writes_the_marker_for_crowd_and_refuses_other_modes(product):
    runner = _real_runner(product)
    crowd = runner.create('crowd_remove', 4, metadata={'camera_model': 'x', 'camera_port': 'usb:0,0'})
    # A session that is not capturing has nothing to stop.
    assert product.client.post(f"/sessions/{crowd['id']}/stop", headers=product.headers).status_code == 400
    running = runner.read(crowd['id'])
    running['status'] = 'running'
    runner.save(running)
    response = product.client.post(f"/sessions/{crowd['id']}/stop", headers=product.headers)
    assert response.status_code == 200, response.text
    assert response.json()['stop_requested'] is True
    assert (runner._directory(crowd['id']) / 'stop').is_file()
    hdr = runner.create('hdr', 3, metadata={'camera_model': 'x', 'camera_port': 'usb:0,0'})
    assert product.client.post(f"/sessions/{hdr['id']}/stop", headers=product.headers).status_code == 400
    assert product.client.post(f"/sessions/{crowd['id']}/stop").status_code == 401
    assert product.client.post('/sessions/' + 'a' * 32 + '/stop', headers=product.headers).status_code == 404


def test_stop_route_refuses_a_queued_job(product):
    runner = _real_runner(product)
    crowd = runner.create('crowd_remove', 4, metadata={'camera_model': 'x', 'camera_port': 'usb:0,0'})

    class Jobs:
        def status(self, session_id):
            return {'id': session_id, 'status': 'queued'}

    product.monkeypatch.setattr(product.app, '_session_jobs', lambda: Jobs())
    response = product.client.post(f"/sessions/{crowd['id']}/stop", headers=product.headers)
    assert response.status_code == 409 and 'use Cancel' in response.text
    assert not (runner._directory(crowd['id']) / 'stop').exists()


def test_preview_every_zero_is_accepted_for_crowd(product):
    _real_runner(product)
    created = product.client.post('/sessions', headers=product.headers, json=_body(product, preview_every=0))
    assert created.status_code == 200, created.text
    assert created.json()['metadata']['preview_every'] == 0


def test_stop_route_requires_the_real_engine(product):
    product_sessions = importlib.import_module('product_sessions')
    stack_session = importlib.import_module('stack_session')
    runner = stack_session.SessionRunner(product.root / 'sessions')
    product.monkeypatch.setattr(product_sessions, 'runner', lambda: runner)
    crowd = runner.create('crowd_remove', 4, metadata={'camera_model': 'x', 'camera_port': 'usb:0,0'})
    assert product.client.post(f"/sessions/{crowd['id']}/stop", headers=product.headers).status_code == 400
    assert not (runner._directory(crowd['id']) / 'stop').exists()


def test_crowd_preview_stream_is_hash_verified(product):
    runner = _real_runner(product)
    state = runner.create('crowd_remove', 4, metadata={'camera_model': 'x', 'camera_port': 'usb:0,0'})
    session_id = state['id']
    assert product.client.get(f'/sessions/{session_id}/crowd-preview', headers=product.headers).status_code == 404
    folder = runner._directory(session_id).resolve() / 'crowd_preview'
    folder.mkdir()
    preview_path = folder / 'preview_0002.jpg'
    data = _jpeg(preview_path, color=(70, 90, 110), size=(320, 240))
    manifest = runner.read(session_id)
    manifest['preview_crowd'] = {'path': str(preview_path), 'bytes': len(data),
                                 'sha256': hashlib.sha256(data).hexdigest()}
    runner.save(manifest)
    served = product.client.get(f'/sessions/{session_id}/crowd-preview', headers=product.headers)
    assert served.status_code == 200, served.text
    assert served.content == data
    preview_path.write_bytes(data + b'tamper')
    assert product.client.get(f'/sessions/{session_id}/crowd-preview', headers=product.headers).status_code == 409
    # A recorded path outside the session directory is never served, including one that only
    # looks contained (dot-dot segments) or reaches out through a symlinked directory.
    outside = product.root / 'outside.jpg'
    outside_data = _jpeg(outside, color=(10, 20, 30), size=(64, 48))
    escape_dir = folder / 'escape'
    escape_dir.symlink_to(product.root, target_is_directory=True)
    for recorded in (str(outside), str(folder) + '/../../../outside.jpg', str(escape_dir / 'outside.jpg')):
        manifest['preview_crowd'] = {'path': recorded, 'bytes': len(outside_data),
                                     'sha256': hashlib.sha256(outside_data).hexdigest()}
        runner.save(manifest)
        response = product.client.get(f'/sessions/{session_id}/crowd-preview', headers=product.headers)
        assert response.status_code == 404, (recorded, response.status_code)
