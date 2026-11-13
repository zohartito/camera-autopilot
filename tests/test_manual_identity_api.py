"""Exact selected-body manual controls without any hardware calls."""
import json
from types import SimpleNamespace

import pytest
from test_final_wave_product_api import product


SERIAL = 'A7II00SERIAL'  # the A7 II's USB serial (lessons: identification)


def _fake_camera(calls, mode, model, port):
    from sonyremote import CameraIdentityError

    class Camera:
        def __init__(self, **kw):
            calls.append(('open', kw)); self.iso = '100'; self.serial = kw.get('serial')
        def __enter__(self):
            # Camera(serial=...) proves the OS USB serial at the port on connect, before any read.
            if self.serial != SERIAL or mode == 'serial_mismatch':
                raise CameraIdentityError('another body holds this port', reason='serial_mismatch',
                                          expected_serial=self.serial, observed_serial='A7III0SERIAL',
                                          model=model, port=port)
            return self
        def __exit__(self, *args): pass
        def info(self):
            return SimpleNamespace(model='wrong' if mode == 'wrong_identity' else model, port=port)
        def get_settings(self):
            calls.append(('read',)); return {'iso': self.iso}
        def set_setting(self, node, value):
            calls.append(('write', node, value))
            if mode == 'write_failure': raise RuntimeError('injected write failure')
            self.iso = value
            return value
    return Camera


@pytest.mark.parametrize('mode', ['missing', 'no_serial', 'serial_mismatch', 'wrong_identity', 'valid', 'write_failure'])
def test_manual_usb_control_identity_and_journal(product, monkeypatch, mode):
    import sonyremote
    model = 'Sony ILCE-7M2 (Control)'
    port = 'usb:000,003'
    calls = []
    monkeypatch.setattr(sonyremote, 'Camera', _fake_camera(calls, mode, model, port))
    monkeypatch.setattr(product.engine, '_free_usb', lambda: None)
    product.fake.name = 'real'
    payload = {'value': '200', 'route': 'usb_gphoto', 'dry_run': True}
    if mode != 'missing': payload.update(camera_model=model, camera_port=port)
    if mode not in ('missing', 'no_serial'): payload['camera_serial'] = SERIAL
    plan = product.client.post('/api/camera/settings/camera.iso', headers=product.headers, json=payload)
    assert plan.status_code == 200, plan.text
    assert plan.json()['identity_bound_live_write'] is (mode not in ('missing', 'no_serial'))
    assert calls == []
    payload['dry_run'] = False
    if mode == 'write_failure':
        with pytest.raises(RuntimeError, match='injected write failure'):
            product.client.post('/api/camera/settings/camera.iso', headers=product.headers, json=payload)
    else:
        response = product.client.post('/api/camera/settings/camera.iso', headers=product.headers, json=payload)
        expected = {'missing': 400, 'no_serial': 400, 'serial_mismatch': 409, 'wrong_identity': 409, 'valid': 200}
        assert response.status_code == expected[mode], response.text
        if mode in ('no_serial', 'serial_mismatch'):
            body = response.json()  # the structured identity refusal, as /shoot returns it
            assert body['error'] == 'camera_identity' and body['may_have_fired'] is False
            assert body['reason'] == {'no_serial': 'serial_required', 'serial_mismatch': 'serial_mismatch'}[mode]
    writes = [row for row in calls if row[0] == 'write']
    assert len(writes) == (1 if mode in ('valid', 'write_failure') else 0)
    if mode == 'no_serial':
        assert calls == []  # refused before any USB open
    if mode == 'serial_mismatch':
        assert [row[0] for row in calls] == ['open'] and calls[0][1]['serial'] == SERIAL  # no read, no write
    records = list((product.root/'manual-controls').glob('*.json'))
    if mode in ('missing', 'no_serial'):
        assert records == []
    else:
        assert len(records) == 1
        receipt = json.loads(records[0].read_text())
        assert receipt['capture_attempts'] == 0 and receipt['camera_serial'] == SERIAL
        assert receipt['status'] == ('completed' if mode == 'valid' else 'failed')
        if mode == 'valid': assert receipt['before']['iso'] == '100' and receipt['after']['iso'] == '200'


@pytest.mark.parametrize('serial, status, reason', [(None, 400, 'serial_required'),
                                                    ('A7III0SERIAL', 409, 'serial_mismatch')])
def test_live_setting_read_requires_the_proven_serial(product, monkeypatch, serial, status, reason):
    import sonyremote
    model, port = 'Sony ILCE-7M2 (Control)', 'usb:000,003'
    calls = []
    monkeypatch.setattr(sonyremote, 'Camera', _fake_camera(calls, 'valid', model, port))
    monkeypatch.setattr(product.engine, '_free_usb', lambda: None)
    monkeypatch.setattr(product.app.settings_control, '_read_usb_node',
                        lambda camera, node: calls.append(('read', node)) or '100')
    product.fake.name = 'real'
    query = {'live': 'true', 'route': 'usb_gphoto', 'camera_model': model, 'camera_port': port}
    if serial: query['camera_serial'] = serial
    response = product.client.get('/api/camera/settings/camera.iso', headers=product.headers, params=query)
    assert response.status_code == status, response.text
    assert response.json()['reason'] == reason
    assert not any(row[0] in ('read', 'write') for row in calls)
    query['camera_serial'] = SERIAL
    response = product.client.get('/api/camera/settings/camera.iso', headers=product.headers, params=query)
    assert response.status_code == 200, response.text
    opens = [row for row in calls if row[0] == 'open']
    assert calls[-1][0] == 'read' and opens[-1][1]['serial'] == SERIAL


def test_registry_listing_and_non_live_reads_need_no_serial(product):
    # Read-only: the registry map and a non-live setting read never open a camera.
    assert product.client.get('/api/camera/settings', headers=product.headers).status_code == 200
    assert product.client.get('/api/camera/settings/camera.iso', headers=product.headers).status_code == 200


def test_mock_cannot_apply_usb_controls(product):
    response = product.client.post('/api/camera/settings/camera.iso', headers=product.headers,
        json={'value':'200','route':'usb_gphoto','dry_run':False,
              'camera_model':'Sony ILCE-7M2 (Control)','camera_port':'usb:000,003'})
    assert response.status_code == 400


def test_registry_does_not_transfer_a7ii_proof_to_a7iii(product):
    product.fake.name = 'real'
    payload = {'value':'200','route':'usb_gphoto','dry_run':True,
               'camera_model':'Sony Alpha-A7 III (PC Control)','camera_port':'usb:000,003'}
    response = product.client.post('/api/camera/settings/camera.iso', headers=product.headers,json=payload)
    assert response.status_code == 200
    assert response.json()['identity_bound_live_write'] is False
    payload['dry_run'] = False
    response = product.client.post('/api/camera/settings/camera.iso', headers=product.headers,json=payload)
    assert response.status_code == 409
    assert 'A7 II-specific' in response.json()['detail']


@pytest.mark.parametrize('live', ['true','1','on','yes','t','y','TRUE'])
def test_all_pydantic_live_true_spellings_share_camera_lock(product, monkeypatch, live):
    def forbidden(*args, **kwargs):
        raise AssertionError('camera read must not begin while camera is occupied')
    monkeypatch.setattr(product.app.settings_control, 'read_setting', forbidden)
    product.app._camera_lock.acquire()
    try:
        response = product.client.get('/api/camera/settings/camera.iso?live='+live, headers=product.headers)
    finally:
        product.app._camera_lock.release()
    assert response.status_code == 409


@pytest.mark.parametrize('route', ['auto', 'smart_remote_wifi'])
def test_live_write_over_a_backend_that_cannot_prove_the_serial_is_refused(product, monkeypatch, route):
    # camera.iso resolves `auto` to the Wi-Fi Smart Remote setter, which reaches whatever body is
    # on the network: this route never writes live over it, even with a serial.
    import sonyremote
    calls = []
    monkeypatch.setattr(sonyremote, 'Camera', _fake_camera(calls, 'valid', 'Sony ILCE-7M2 (Control)', 'usb:000,003'))
    product.fake.name = 'real'
    response = product.client.post('/api/camera/settings/camera.iso', headers=product.headers, json={
        'value': '200', 'route': route, 'dry_run': False, 'camera_model': 'Sony ILCE-7M2 (Control)',
        'camera_port': 'usb:000,003', 'camera_serial': SERIAL})
    assert response.status_code == 409, response.text
    assert 'cannot prove' in response.json()['detail'] and calls == []


def test_a_vanished_port_is_a_503_not_a_500(product, monkeypatch):
    import sonyremote
    from sonyremote import CameraError

    class Gone:
        def __init__(self, **kw): pass
        def __enter__(self): raise CameraError('No camera at usb:000,003')
        def __exit__(self, *args): pass
    monkeypatch.setattr(sonyremote, 'Camera', Gone)
    monkeypatch.setattr(product.engine, '_free_usb', lambda: None)
    product.fake.name = 'real'
    response = product.client.post('/api/camera/settings/camera.iso', headers=product.headers, json={
        'value': '200', 'route': 'usb_gphoto', 'dry_run': False, 'camera_model': 'Sony ILCE-7M2 (Control)',
        'camera_port': 'usb:000,003', 'camera_serial': SERIAL})
    assert response.status_code == 503 and 'No camera' in response.json()['detail']


@pytest.mark.parametrize('route', ['auto', 'smart_remote_wifi'])
def test_a_non_usb_live_write_without_a_serial_is_the_structured_serial_required_refusal(product, route):
    # Review thread on #88: the serial check comes before any live dispatch, USB or not.
    product.fake.name = 'real'
    response = product.client.post('/api/camera/settings/camera.iso', headers=product.headers, json={
        'value': '200', 'route': route, 'dry_run': False, 'camera_model': 'Sony ILCE-7M2 (Control)',
        'camera_port': 'usb:000,003'})
    assert response.status_code == 400, response.text
    body = response.json()
    assert body['error'] == 'camera_identity' and body['reason'] == 'serial_required'
    assert body['may_have_fired'] is False
