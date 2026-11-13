"""Section 1 route proofs: mood previews, handheld sessions, and panorama session extras.

Camera-free: the real-engine paths patch ``sonyremote.camera.Camera``, ``engine._free_usb`` and
``handheld.HandheldSession`` so no USB body is ever opened. Reuses the ``product`` fixture from
test_final_wave_product_api (TestClient + token + mock engine + isolated gallery).
"""
import hashlib
import importlib
import threading
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from test_final_wave_product_api import product  # noqa: F401  (pytest fixture import)


def _jpeg(path: Path, color=(90, 110, 130), size=(160, 100)) -> bytes:
    Image.new('RGB', size, color).save(path, 'JPEG', quality=90)
    return path.read_bytes()


# --------------------------------------------------------------------------- mood previews
def test_mood_preview_requires_token(product):
    _jpeg(product.root / 'scene.jpg')
    assert product.client.get('/moods/preview', params={'image': 'scene.jpg'}).status_code == 401


@pytest.mark.parametrize('name', ['../x.jpg', 'shot.json', 'missing.jpg'])
def test_mood_preview_rejects_unsafe_or_missing_name(product, name):
    response = product.client.get('/moods/preview', headers=product.headers, params={'image': name})
    assert response.status_code == 404, response.text


def test_mood_preview_renders_one_dataurl_per_variant_with_recipes(product):
    import intent
    _jpeg(product.root / 'scene.jpg')
    response = product.client.get('/moods/preview', headers=product.headers, params={'image': 'scene.jpg'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['image'] == 'scene.jpg'
    variant_ids = {variant['id'] for variant in intent.MOOD_VARIANTS}
    assert set(body['previews']) == variant_ids
    assert all(url.startswith('data:image/jpeg;base64,') for url in body['previews'].values())
    assert body['recipes'] == {variant['id']: variant['recipe'] for variant in intent.MOOD_VARIANTS}
    # Deterministic: a second render of the same source is byte-identical.
    again = product.client.get('/moods/preview', headers=product.headers, params={'image': 'scene.jpg'})
    assert again.json() == body


# --------------------------------------------------------------------------- handheld under mock engine
def test_handheld_under_mock_engine_is_rejected_and_unknown_ids_404(product):
    body = {'camera_model': sorted(product.engine.USB_CONTROL_MODELS)[0], 'camera_port': 'usb:000,003'}
    assert product.client.post('/handheld', headers=product.headers, json=body).status_code == 400
    assert product.client.get('/handheld/' + 'a' * 32, headers=product.headers).status_code == 404
    assert product.client.post('/handheld/' + 'a' * 32 + '/cancel', headers=product.headers).status_code == 404


# --------------------------------------------------------------------------- handheld under a patched real engine
class _FakeCamera:
    def __init__(self, model=None, port=None, serial=None):
        self.model, self.port, self.serial = model, port, serial

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def list_options(self, key):
        return {'iso': ['100', '200', '400'], 'aperture': ['2.8', '4.0', '5.6'],
                'shutter': ['1/60', '1/125', '1/250']}[key]


class _FakeHandheld:
    """Writes one receipted frame synchronously, then blocks ``run`` on the cancel event."""

    def __init__(self, factory, recommend, *, max_frames, max_duration_s, out_dir):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        frame = self.out_dir / 'frame_0001.jpg'
        data = _jpeg(frame, color=(40, 60, 80))
        self._receipt = {'path': str(frame), 'bytes': len(data),
                         'sha256': hashlib.sha256(data).hexdigest()}
        self._cancel = None

    def run(self, cancel_event):
        self._cancel = cancel_event
        cancel_event.wait(5)
        return self.state()

    def state(self):
        cancelled = self._cancel is not None and self._cancel.is_set()
        return {'status': 'cancelled' if cancelled else 'running',
                'frames': [{'receipt': self._receipt}], 'applied_count': 1}


def _patch_real_handheld(product):
    handheld = importlib.import_module('handheld')
    camera_mod = importlib.import_module('sonyremote.camera')
    product.fake.name = 'real'
    product.app._handheld_sessions.clear()
    product.monkeypatch.setattr(camera_mod, 'Camera', _FakeCamera)
    product.monkeypatch.setattr(product.engine, '_free_usb', lambda: None)
    product.monkeypatch.setattr(handheld, 'HandheldSession', _FakeHandheld)


def test_handheld_invalid_identity_rejected_before_probe(product):
    _patch_real_handheld(product)
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    assert product.client.post('/handheld', headers=product.headers,
                               json={'camera_model': model, 'camera_port': 'usb:x'}).status_code == 400
    assert product.client.post('/handheld', headers=product.headers,
                               json={'camera_model': 'Unlisted body', 'camera_port': 'usb:000,003'}).status_code == 400


def test_handheld_real_lifecycle_frames_busy_and_cancel_release(product):
    _patch_real_handheld(product)
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    body = {'camera_model': model, 'camera_port': 'usb:000,003', 'camera_serial': 'A7III0SERIAL'}

    started = product.client.post('/handheld', headers=product.headers, json=body)
    assert started.status_code == 202, started.text
    session_id = started.json()['id']

    # Second concurrent start is refused while the first session is running.
    assert product.client.post('/handheld', headers=product.headers, json=body).status_code == 409

    status = product.client.get('/handheld/' + session_id, headers=product.headers)
    assert status.status_code == 200
    payload = status.json()
    assert payload['camera_model'] == model
    assert 'intent' in payload and payload['status'] == 'running'

    # The hash-verified frame streams the exact bytes.
    frame_path = product.root / 'handheld' / session_id / 'frame_0001.jpg'
    expected = frame_path.read_bytes()
    stream = product.client.get(f'/handheld/{session_id}/frames/frame_0001.jpg', headers=product.headers)
    assert stream.status_code == 200
    assert stream.content == expected
    with Image.open(BytesIO(stream.content)) as decoded:
        assert decoded.format == 'JPEG'

    # Altering the file breaks the receipt: the stream is refused, not served.
    frame_path.write_bytes(expected + b'tamper')
    assert product.client.get(f'/handheld/{session_id}/frames/frame_0001.jpg',
                              headers=product.headers).status_code == 409

    # Cancel requests a stop; once the worker exits the camera lock is free again.
    cancel = product.client.post('/handheld/' + session_id + '/cancel', headers=product.headers)
    assert cancel.status_code == 200
    assert cancel.json()['cancel_requested'] is True
    product.app._handheld_sessions[session_id]['thread'].join(timeout=5)
    assert not product.app._camera_lock.locked()

    # A fresh session is accepted after the previous thread exits (not a 409).
    reopened = product.client.post('/handheld', headers=product.headers, json=body)
    assert reopened.status_code == 202, reopened.text
    new_id = reopened.json()['id']
    product.app._handheld_sessions[new_id]['cancel'].set()
    product.app._handheld_sessions[new_id]['thread'].join(timeout=5)


# --------------------------------------------------------------------------- sessions: panorama + preview
def _real_runner(product):
    product_sessions = importlib.import_module('product_sessions')
    stack_session = importlib.import_module('stack_session')
    runner = stack_session.SessionRunner(product.root / 'sessions')
    product.monkeypatch.setattr(product_sessions, 'runner', lambda: runner)
    product.fake.name = 'real'
    return runner


def test_session_create_folds_panorama_and_preview_every(product):
    _real_runner(product)
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    base = {'mode': 'panorama', 'camera_model': model, 'camera_port': 'usb:000,003', 'camera_serial': 'A7III0SERIAL',
            'panorama': {'projection': 'cylindrical'}, 'preview_every': 1}
    created = product.client.post('/sessions', headers=product.headers, json={**base, 'frames': 3})
    assert created.status_code == 200, created.text
    metadata = created.json()['metadata']
    assert metadata['panorama'] == {'projection': 'cylindrical'}
    assert metadata['preview_every'] == 1
    # A panorama over the frame ceiling is rejected.
    assert product.client.post('/sessions', headers=product.headers,
                               json={**base, 'frames': 13}).status_code == 400


def test_session_panorama_preview_stream_is_hash_verified(product):
    runner = _real_runner(product)
    state = runner.create('panorama', 3, metadata={'camera_model': 'x', 'camera_port': 'usb:0,0'})
    session_id = state['id']

    # No preview recorded yet.
    assert product.client.get(f'/sessions/{session_id}/panorama-preview',
                              headers=product.headers).status_code == 404

    directory = runner._directory(session_id).resolve()
    preview_path = directory / 'preview_panorama.jpg'
    data = _jpeg(preview_path, color=(70, 90, 110), size=(320, 120))
    manifest = runner.read(session_id)
    manifest['preview_panorama'] = {'path': str(preview_path), 'bytes': len(data),
                                    'sha256': hashlib.sha256(data).hexdigest()}
    runner.save(manifest)

    served = product.client.get(f'/sessions/{session_id}/panorama-preview', headers=product.headers)
    assert served.status_code == 200, served.text
    assert served.content == data

    # A post-acceptance change to the file is refused.
    preview_path.write_bytes(data + b'tamper')
    assert product.client.get(f'/sessions/{session_id}/panorama-preview',
                              headers=product.headers).status_code == 409


def _night_meter_jpeg(product, name='night_meter.jpg', value=118):
    """A mid-tone gallery JPEG with the EXIF exposure the night planner meters from."""
    exif = Image.Exif()
    sub = {34855: 1600, 33437: 4.0, 33434: 0.5, 42036: 'FE 24-105mm F4 G OSS', 42034: (24.0, 105.0, 4.0, 4.0)}
    for tag, val in sub.items():
        exif[tag] = val
    image = Image.new('RGB', (96, 64), (value, value, value))
    image.save(product.root / name, quality=92, exif=exif)
    return name


def test_night_assist_plan_requires_real_engine_and_safe_name(product):
    name = _night_meter_jpeg(product)
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    body = {'image': name, 'camera_model': model, 'camera_port': 'usb:000,003'}
    assert product.client.post('/night-assist/plan', headers=product.headers, json=body).status_code == 400
    _patch_real_handheld(product)
    bad = dict(body, image='../x.jpg')
    assert product.client.post('/night-assist/plan', headers=product.headers, json=bad).status_code == 404
    assert product.client.post('/night-assist/plan', json=body).status_code == 401


def test_night_assist_plan_returns_session_request_from_ladders(product):
    _patch_real_handheld(product)
    name = _night_meter_jpeg(product)
    model = 'Sony Alpha-A7 III (PC Control)'
    body = {'image': name, 'camera_model': model, 'camera_port': 'usb:001,004'}
    response = product.client.post('/night-assist/plan', headers=product.headers, json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['image'] == name and plan['status'] in ('ok', 'insufficient_light', 'meter_unreliable')
    assert isinstance(plan['why'], list) and plan['evidence']
    if plan['status'] != 'ok':  # the mock ladders may not reach the scene; the refusal must be explicit
        assert plan['session_request'] is None and plan['shortfall_ev'] is not None or plan['status'] == 'meter_unreliable'
    if plan['status'] == 'ok':
        request = plan['session_request']
        assert request['mode'] == 'night' and 2 <= request['frames'] <= 16
        assert request['night_assist']['frames'] == request['frames']
        created = product.client.post('/sessions', headers=product.headers, json=request)
        assert created.status_code in (200, 400), created.text  # mock engine refuses real sessions


class _WideLadderCamera(_FakeCamera):
    """Ladders wide enough for the mid-tone meter frame to plan an ``ok`` night stack."""

    def list_options(self, key):
        return {'iso': ['100', '200', '400', '800', '1600', '3200', '6400'], 'aperture': ['f/4', 'f/5.6', 'f/8'],
                'shutter': ['1', '1/2', '1/4', '1/8', '1/15', '1/30', '1/60']}[key]


def test_night_assist_session_request_carries_the_serial_and_is_accepted(product):
    """/sessions refuses a body without the USB serial (serial_required); the planner's ready
    request must carry the selected serial so it is accepted as returned."""
    _patch_real_handheld(product)
    product.monkeypatch.setattr(importlib.import_module('sonyremote.camera'), 'Camera', _WideLadderCamera)
    name = _night_meter_jpeg(product)
    body = {'image': name, 'camera_model': 'Sony Alpha-A7 III (PC Control)', 'camera_port': 'usb:001,004',
            'camera_serial': 'A7III0SERIAL'}
    response = product.client.post('/night-assist/plan', headers=product.headers, json=body)
    assert response.status_code == 200, response.text
    plan = response.json()
    assert plan['status'] == 'ok', plan
    request = plan['session_request']
    assert request['camera_serial'] == 'A7III0SERIAL'
    assert request['night_assist']['camera_serial'] == 'A7III0SERIAL'  # the plan records its body
    created = product.client.post('/sessions', headers=product.headers, json=request)
    assert created.status_code == 200, created.text
    assert created.json()['metadata']['camera_serial'] == 'A7III0SERIAL'
    assert created.json()['metadata']['night_assist']['frames'] == request['frames']


def test_session_night_assist_metadata_is_validated_at_create(product):
    _patch_real_handheld(product)
    model = 'Sony Alpha-A7 III (PC Control)'
    good = {'iso': '100', 'aperture_label': 'f/4', 'shutter_label': '1', 'frames': 2}
    base = {'frames': 2, 'camera_model': model, 'camera_port': 'usb:001,004'}
    wrong_mode = product.client.post('/sessions', headers=product.headers, json={**base, 'mode': 'hdr', 'night_assist': good})
    assert wrong_mode.status_code == 400
    mismatch = product.client.post('/sessions', headers=product.headers, json={**base, 'mode': 'night', 'night_assist': {**good, 'frames': 4}})
    assert mismatch.status_code == 400
    malformed = product.client.post('/sessions', headers=product.headers, json={**base, 'mode': 'night', 'night_assist': {'iso': 'AUTO'}})
    assert malformed.status_code == 422
