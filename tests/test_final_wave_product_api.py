"""Product HTTP regressions using isolated galleries and camera-free adapters."""
from __future__ import annotations

import importlib
import json
import sys
import threading
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


@pytest.fixture
def product(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMERA_ENGINE', 'mock')
    module = importlib.import_module('app')
    engine = importlib.import_module('engine')
    intent_brain = importlib.import_module('intent_brain')
    root = tmp_path / 'shots'
    root.mkdir()
    monkeypatch.setattr(module, 'SHOTS_DIR', root)
    monkeypatch.setattr(engine, 'SHOTS_DIR', root)
    monkeypatch.setattr(module, 'API_TOKEN', 'fixture-credential-1234567890')
    monkeypatch.setattr(module, '_camera_lock', threading.Lock())
    monkeypatch.setattr(module, '_capture_budget', module.RequestBudget(limit=100))
    monkeypatch.setattr(intent_brain, 'interpret', lambda intent, **kw: {
        'target_brightness': 112, 'white_balance': None, 'why': 'fixture plan'})
    calls = []

    class FixtureEngine:
        name = 'mock'

        def shoot(self, intent=None, **kwargs):
            calls.append({'intent': intent, **kwargs})
            name = f'shot_fixture_{len(calls)}.jpg'
            Image.new('RGB', (160, 100), (80, 90, 100)).save(root / name)
            return engine.Shot(ok=True, image=name, summary='Fixture capture', why='Camera-free adapter',
                               mood={}, settings={'iso': '100'}, cards=[{'axis': 'motion', 'status': 'unknown'}],
                               evidence={'source': 'fixture'})

    fake = FixtureEngine()
    monkeypatch.setattr(module, 'engine', fake)
    headers = {'X-Camera-Autopilot-Token': module.API_TOKEN}
    # Startup retention runs only against this temporary gallery.
    with TestClient(module.app) as client:
        yield SimpleNamespace(app=module, engine=engine, fake=fake, root=root, calls=calls,
                              client=client, headers=headers, monkeypatch=monkeypatch)


def shoot(product, **payload):
    response = product.client.post('/shoot', json=payload, headers=product.headers)
    assert response.status_code == 200, response.text
    return response.json()


def test_capture_cards_thumbnail_edit_delete_preserves_original(product):
    result = shoot(product, intent='fixture')
    image = result['image']
    original = (product.root / image).read_bytes()
    cards = product.client.get(f'/shots/{image}/cards', headers=product.headers)
    assert cards.status_code == 200
    assert cards.json()['cards'][0]['axis'] == 'motion'
    thumbnail = product.client.get(f'/thumbnails/{image}', headers=product.headers)
    assert thumbnail.status_code == 200
    assert thumbnail.headers['content-type'] == 'image/jpeg'
    with Image.open(BytesIO(thumbnail.content)) as decoded:
        assert decoded.width <= 640 and decoded.height <= 640
    edit = product.client.post('/edit', headers=product.headers,
                               json={'image': image, 'exposure_ev': 1, 'crop': [0, 0, .5, 1]})
    assert edit.status_code == 200, edit.text
    edited = edit.json()['image']
    assert edited != image
    assert (product.root / image).read_bytes() == original
    assert (product.root / '.lifecycle' / f'{edited}.json').is_file()
    with Image.open(product.root / edited) as decoded:
        assert decoded.size == (80, 100)
        assert decoded.getpixel((20, 20))[0] > 80
    deleted = product.client.delete(f'/photos/{edited}', headers=product.headers)
    assert deleted.status_code == 200, deleted.text  # no Content-Length on bodyless DELETE
    assert not (product.root / edited).exists()
    assert not (product.root / f'{Path(edited).stem}.json').exists()
    assert (product.root / image).read_bytes() == original


@pytest.mark.parametrize('method,path,body', [
    ('get', '/gallery', None), ('get', '/retention', None),
    ('get', '/shots/fixture.jpg/cards', None), ('get', '/thumbnails/fixture.jpg', None),
    ('delete', '/photos/fixture.jpg', None), ('delete', '/photos', {'confirm': 'delete-all-local-photos'}),
    ('put', '/retention', {'days': 7}), ('post', '/edit', {'image': 'fixture.jpg'}),
])
def test_private_photo_endpoints_require_token(product, method, path, body):
    kwargs = {} if body is None else {'json': body}
    response = product.client.request(method, path, **kwargs)
    assert response.status_code == 401
    assert not list(product.root.iterdir())


def test_retention_explicit_setting_and_confirmation(product):
    result = shoot(product)
    assert product.client.get('/retention', headers=product.headers).json()['days'] is None
    chosen = product.client.put('/retention', headers=product.headers, json={'days': 7})
    assert chosen.status_code == 200
    assert chosen.json()['preserves_existing_unenrolled_photos']
    for value in [0, True, '7']:
        rejected = product.client.put('/retention', headers=product.headers, json={'days': value})
        assert rejected.status_code in {400, 422}, (value, rejected.text)
    denied = product.client.request('DELETE', '/photos', headers=product.headers, json={'confirm': 'yes'})
    assert denied.status_code == 400
    assert (product.root / result['image']).is_file()
    accepted = product.client.request('DELETE', '/photos', headers=product.headers,
                                      json={'confirm': 'delete-all-local-photos'})
    assert accepted.status_code == 200
    assert not (product.root / result['image']).exists()


def test_actual_body_overflow_rejected_with_false_small_header(product):
    response = product.client.post('/shoot', headers={**product.headers, 'Content-Length': '2',
        'Content-Type': 'application/json'}, content=b' ' * (product.app.MAX_API_BODY_BYTES + 1))
    assert response.status_code == 413
    assert not product.calls


def test_refinement_is_bounded_and_applies_corrected_target(product):
    product.fake.name = 'fixture_real'
    vision = importlib.import_module('vision_critique')
    product.monkeypatch.setattr(vision, 'critique', lambda *args: {
        'exposure': 'too dark', 'brightness_delta': 20, 'issues': ['dark']})
    result = shoot(product, intent='brighter', refine=True, variant_id='warm')
    assert len(product.calls) == 2
    assert [call['target_override'] for call in product.calls] == [112, 132]
    assert all(call['variant_id'] == 'warm' for call in product.calls)
    assert result['evidence']['refinement']['attempts'] == 2
    assert result['evidence']['refinement']['stopped_reason'] == 'max_attempts'
    saved = json.loads((product.root / f"{Path(result['image']).stem}.json").read_text())
    assert saved['evidence']['refinement']['attempts'] == 2


def test_mock_refinement_never_calls_vision(product):
    vision = importlib.import_module('vision_critique')
    def forbidden(*args, **kwargs):
        pytest.fail('Mock capture must not send an image to a model')
    product.monkeypatch.setattr(vision, 'critique', forbidden)
    result = shoot(product, refine=True)
    assert len(product.calls) == 1
    assert result['evidence']['refinement']['stopped_reason'] == 'no_critique'


def test_exact_camera_selection_is_passed_without_hardware(product):
    product.fake.name = 'real'
    selections = []
    def factory(**kwargs):
        selections.append(kwargs)
        return product.fake
    product.monkeypatch.setattr(product.engine, 'RealEngine', factory)
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    shoot(product, camera_model=model, camera_port='usb:fixture,001', camera_serial='A7III0SERIAL')
    assert selections == [{'camera_model': model, 'camera_port': 'usb:fixture,001', 'camera_serial': 'A7III0SERIAL'}]
    bad = product.client.post('/shoot', headers=product.headers, json={'camera_model': 'Unlisted camera'})
    assert bad.status_code == 400
    assert len(selections) == 1


def test_mock_selection_and_session_run_cannot_open_usb(product):
    sessions = importlib.import_module('product_sessions')
    def forbidden(*args, **kwargs):
        pytest.fail('Mock API must not enter the USB session adapter')
    product.monkeypatch.setattr(sessions, 'run', forbidden)
    product.monkeypatch.setattr(product.engine, 'RealEngine', forbidden)
    assert product.client.post('/shoot', headers=product.headers,
        json={'camera_model': sorted(product.engine.USB_CONTROL_MODELS)[0]}).status_code == 400
    assert product.client.post('/sessions/' + 'a' * 32 + '/run', headers=product.headers).status_code == 400
    assert not product.calls


def test_busy_camera_blocks_capture_session_and_setting_mutation(product):
    sessions = importlib.import_module('product_sessions')
    product.fake.name = 'real'
    def forbidden(*args, **kwargs):
        pytest.fail('Busy camera operation must not enter hardware adapter')
    product.monkeypatch.setattr(sessions, 'run', forbidden)
    product.monkeypatch.setattr(product.app.settings_control, 'apply_setting', forbidden)
    product.monkeypatch.setattr(product.engine, 'RealEngine', lambda **kwargs: product.fake)
    product.fake.record = forbidden
    product.app._camera_lock.acquire()
    try:
        assert product.client.post('/shoot', headers=product.headers, json={}).status_code == 409
        assert product.client.post('/sessions/' + 'a' * 32 + '/run', headers=product.headers).status_code == 409
        assert product.client.post('/record', headers=product.headers, json={
            'camera_model': sorted(product.engine.USB_CONTROL_MODELS)[0],
            'camera_port': 'usb:fixture,001', 'camera_serial': 'A7III0SERIAL'}).status_code == 409
        assert product.client.post('/api/camera/settings/iso', headers=product.headers,
            json={'value': '100', 'dry_run': False}).status_code == 409
    finally:
        product.app._camera_lock.release()
    assert not product.calls
    product.fake.name = 'mock'
    assert shoot(product)['ok']


def test_rate_limit_rejects_before_capture(product):
    product.monkeypatch.setattr(product.app, '_capture_budget', product.app.RequestBudget(limit=1))
    shoot(product)
    response = product.client.post('/shoot', headers=product.headers, json={})
    assert response.status_code == 429
    assert int(response.headers['retry-after']) > 0
    assert len(product.calls) == 1


@pytest.mark.parametrize('refine', [False, True])
@pytest.mark.parametrize('failure', [OSError('fixture disk full'), ValueError('fixture lifecycle failure')])
def test_saved_photo_metadata_failure_never_recaptures(product, refine, failure):
    product.fake.name = 'fixture_real'
    vision = importlib.import_module('vision_critique')
    def fail_save(*args, **kwargs):
        raise failure
    def forbidden(*args, **kwargs):
        pytest.fail('Metadata failure must stop corrective reshooting before critique')
    product.monkeypatch.setattr(product.engine, 'save_shot_record', fail_save)
    product.monkeypatch.setattr(vision, 'critique', forbidden)
    result = shoot(product, refine=refine)
    assert len(product.calls) == 1
    assert result['evidence']['persistence'] == {
        'status': 'failed', 'retry_capture': False, 'reason': type(failure).__name__}
    with Image.open(product.root / result['image']) as image:
        assert image.size == (160, 100)
    if refine:
        assert result['evidence']['refinement']['attempts'] == 1
    delivered = product.client.get('/shots/' + result['image'], headers=product.headers)
    assert delivered.status_code == 200


def test_partial_usb_selectors_rejected_before_adapter(product):
    product.fake.name = 'real'
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    def forbidden(*args, **kwargs):
        pytest.fail('Partial camera identity must not construct an adapter')
    product.monkeypatch.setattr(product.engine, 'RealEngine', forbidden)
    for payload in [{}, {'camera_model': model}, {'camera_port': 'usb:fixture,001'}]:
        result = product.client.post('/shoot', headers=product.headers, json=payload)
        assert result.status_code == 400, result.text
    assert not product.calls


def test_configured_usb_identity_is_used_when_request_omits_pair(product):
    product.fake.name = 'real'
    product.fake.camera_model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    product.fake.camera_port = 'usb:fixture,001'
    product.fake.camera_serial = 'A7III0SERIAL'
    selections = []
    def factory(**kwargs):
        selections.append(kwargs)
        return product.fake
    product.monkeypatch.setattr(product.engine, 'RealEngine', factory)
    shoot(product)
    assert selections == [{'camera_model': product.fake.camera_model,
                            'camera_port': product.fake.camera_port,
                            'camera_serial': product.fake.camera_serial}]


def test_session_creation_requires_complete_identity_and_has_bounds(product):
    product.fake.name = 'real'
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    for payload in [
        {'mode': 'hdr', 'camera_model': model},
        {'mode': 'hdr', 'camera_port': 'usb:fixture,001'},
        {'mode': 'timelapse', 'camera_model': model, 'camera_port': 'usb:fixture,001',
         'camera_serial': 'A7III0SERIAL', 'frames': 10, 'interval_s': 500},
        {'mode': 'hdr', 'frames': 3, 'camera_model': model, 'camera_port': 'usb:fixture,001'},
    ]:
        assert product.client.post('/sessions', headers=product.headers, json=payload).status_code == 400
    created = product.client.post('/sessions', headers=product.headers,
        json={'mode': 'hdr', 'frames': 3, 'camera_model': model, 'camera_port': 'usb:fixture,001',
              'camera_serial': 'A7III0SERIAL'})
    assert created.status_code == 200, created.text
    result = created.json()
    assert result['status'] == 'ready'
    assert result['metadata'] == {'camera_model': model, 'camera_port': 'usb:fixture,001',
                                  'camera_serial': 'A7III0SERIAL'}
    assert result['frame_count'] == 3
    assert not product.calls


def test_only_a_holy_grail_session_may_be_planned_beyond_one_hour(product):
    product.fake.name = 'real'
    model = sorted(product.engine.USB_CONTROL_MODELS)[0]
    base = {'mode': 'timelapse', 'camera_model': model, 'camera_port': 'usb:fixture,001',
            'camera_serial': 'A7III0SERIAL', 'interval_s': 30}
    two_hours = {**base, 'frames': 240, 'timelapse_mode': 'holy_grail'}  # 239 gaps x 30 s ~ 2 h
    assert product.client.post('/sessions', headers=product.headers, json=two_hours).status_code == 200
    rejected = product.client.post('/sessions', headers=product.headers,
                                   json={**two_hours, 'timelapse_mode': 'auto_custom'})
    assert rejected.status_code == 400 and '1 hour' in rejected.text
    too_long = {**base, 'frames': 200, 'interval_s': 60, 'timelapse_mode': 'holy_grail'}  # 3.3 h
    assert product.client.post('/sessions', headers=product.headers, json=too_long).status_code == 400
    stack = {'mode': 'hdr', 'frames': 150, 'camera_model': model, 'camera_port': 'usb:fixture,001',
             'camera_serial': 'A7III0SERIAL'}
    assert product.client.post('/sessions', headers=product.headers, json=stack).status_code == 400
    assert not product.calls


def test_request_cannot_mix_partial_identity_with_configured_other_body(product):
    product.fake.name = 'real'
    models = sorted(product.engine.USB_CONTROL_MODELS)
    product.fake.camera_model = models[0]
    product.fake.camera_port = 'usb:configured,001'
    def forbidden(*args, **kwargs):
        pytest.fail('Partial request identity must not mix with a configured body')
    product.monkeypatch.setattr(product.engine, 'RealEngine', forbidden)
    for payload in [{'camera_model': models[-1]}, {'camera_port': 'usb:different,002'}]:
        response = product.client.post('/shoot', headers=product.headers, json=payload)
        assert response.status_code == 400, response.text
    assert not product.calls


def test_refinement_reuses_one_interpretation_with_independent_plan_copies(product):
    import copy
    intent_brain = importlib.import_module('intent_brain')
    vision = importlib.import_module('vision_critique')
    original_plan = {'target_brightness': 112, 'white_balance': 'Daylight',
                     'why': 'one plan', 'nested': {'recipe': ['warm']}}
    interpretations, received_plans, targets = [], [], []
    def interpret(intent, **kwargs):
        interpretations.append((intent, kwargs))
        return original_plan
    product.monkeypatch.setattr(intent_brain, 'interpret', interpret)
    product.monkeypatch.setattr(vision, 'critique', lambda *args: {
        'exposure': 'too dark', 'brightness_delta': 20, 'issues': ['dark']})
    baseline_shoot = product.fake.shoot
    product.fake.name = 'fixture_real'
    def planned_shoot(intent=None, *, applied_plan=None, target_override=None, variant_id=None):
        received_plans.append(copy.deepcopy(applied_plan))
        targets.append(target_override)
        # Mutation by one engine attempt must not change the next attempt's plan.
        applied_plan['nested']['recipe'].append('attempt-local')
        applied_plan['target_brightness'] = 1
        return baseline_shoot(intent, target_override=target_override, variant_id=variant_id)
    product.monkeypatch.setattr(product.fake, 'shoot', planned_shoot)
    result = shoot(product, intent='warm scene', variant_id='warm', refine=True)
    assert interpretations == [('warm scene', {'variant_id': 'warm'})]
    assert len(received_plans) == 2
    assert received_plans[0] == received_plans[1] == original_plan
    assert original_plan['nested']['recipe'] == ['warm']
    assert original_plan['target_brightness'] == 112
    assert targets == [112, 132]
    assert result['evidence']['refinement']['attempts'] == 2


def test_lifecycle_failure_after_sidecar_still_delivers_one_photo(product):
    lifecycle = importlib.import_module('photo_lifecycle')
    product.fake.name = 'fixture_real'
    def fail_enrollment(*args, **kwargs):
        raise OSError('fixture lifecycle store unavailable')
    product.monkeypatch.setattr(lifecycle, 'manage_shot', fail_enrollment)
    result = shoot(product, refine=True)
    assert len(product.calls) == 1
    assert result['evidence']['persistence']['retry_capture'] is False
    assert result['evidence']['refinement']['attempts'] == 1
    assert (product.root / result['image']).is_file()
    assert (product.root / f"{Path(result['image']).stem}.json").is_file()
    assert product.client.get('/shots/' + result['image'], headers=product.headers).status_code == 200


def test_transient_persistence_failure_recovers_saved_receipt_without_recapture(product):
    product.fake.name = 'fixture_real'
    save = product.engine.save_shot_record
    persist_attempts = []
    def transient_save(shot, intent):
        persist_attempts.append(shot.image)
        if len(persist_attempts) == 1:
            raise OSError('fixture transient disk error')
        return save(shot, intent)
    vision = importlib.import_module('vision_critique')
    def forbidden(*args, **kwargs):
        pytest.fail('Persistence recovery must not trigger a corrective capture')
    product.monkeypatch.setattr(product.engine, 'save_shot_record', transient_save)
    product.monkeypatch.setattr(vision, 'critique', forbidden)
    result = shoot(product, refine=True)
    assert len(product.calls) == 1
    assert len(persist_attempts) == 2
    assert result['evidence']['persistence']['status'] == 'recovered'
    assert result['evidence']['persistence']['retry_capture'] is False
    saved = json.loads((product.root / f"{Path(result['image']).stem}.json").read_text())
    assert saved['evidence']['persistence']['status'] == 'recovered'
    assert saved['evidence']['refinement']['attempts'] == 1


@pytest.mark.parametrize('refine', [False, True])
def test_delete_all_waits_for_capture_publication_without_orphan_metadata(product, refine):
    from concurrent.futures import ThreadPoolExecutor
    lifecycle = importlib.import_module('photo_lifecycle')
    copied = threading.Event()
    finish_capture = threading.Event()
    delete_started = threading.Event()
    delete_finished = threading.Event()
    capture = product.fake.shoot
    delete_all = lifecycle.PhotoLifecycle.delete_all

    def paused_capture(intent=None, **kwargs):
        shot = capture(intent, **kwargs)
        copied.set()
        assert finish_capture.wait(3), 'test capture barrier timed out'
        return shot

    def observed_delete(self, **kwargs):
        delete_started.set()
        result = delete_all(self, **kwargs)
        delete_finished.set()
        return result

    product.monkeypatch.setattr(product.fake, 'shoot', paused_capture)
    product.monkeypatch.setattr(lifecycle.PhotoLifecycle, 'delete_all', observed_delete)
    with ThreadPoolExecutor(max_workers=2) as executor:
        capture_job = executor.submit(product.client.post, '/shoot', headers=product.headers,
                                      json={'refine': refine})
        assert copied.wait(3)
        delete_job = executor.submit(product.client.request, 'DELETE', '/photos',
                                    headers=product.headers, json={'confirm': 'delete-all-local-photos'})
        try:
            assert delete_started.wait(3)
            assert not delete_finished.wait(.1)
        finally:
            finish_capture.set()
        captured = capture_job.result(timeout=3)
        deleted = delete_job.result(timeout=3)
    assert captured.status_code == 200, captured.text
    assert deleted.status_code == 200, deleted.text
    assert len(product.calls) == 1
    image = captured.json()['image']
    removed = deleted.json()['photos'][0]['files']
    assert image in removed
    assert f'{Path(image).stem}.json' in removed
    assert f'.lifecycle/{image}.json' in removed
    assert not (product.root / image).exists()
    assert not (product.root / f'{Path(image).stem}.json').exists()
    assert not (product.root / '.lifecycle' / f'{image}.json').exists()
    assert 'persistence' not in captured.json()['evidence']
