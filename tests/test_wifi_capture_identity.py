"""Exercise actual Wi-Fi orchestration with controlled camera responses."""
from pathlib import Path
import json
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from types import SimpleNamespace

import pytest
from PIL import Image
import wifi_autopilot
import capture_analysis
from sonyremote import CaptureOutcomeUncertain


class Camera:
    def __init__(self, fail=False): self.fail = fail; self.downloads = []; self.calls = []; self.shutters = 0
    def begin_session(self): pass
    def set_focus_mode(self, mode): pass
    def grab_liveview_frame(self, save_path): Image.new('RGB', (32, 24), 'gray').save(save_path)
    def focal_length_mm(self, default): return 50
    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'setWhiteBalance':
            self.white_balance = list(params or ['Color Temperature', True, 7500])
            return {'result': [0]}
        if method == 'getWhiteBalance':
            # A real body echoes the last accepted write; the fail-closed verifier depends on that.
            return {'result': list(getattr(self, 'white_balance', ['Color Temperature', True, 7500]))}
        if method == 'getAvailableWhiteBalance':
            return {'result': [{'whiteBalanceMode': 'Auto WB'}, {'whiteBalanceMode': 'Color Temperature', 'colorTemperatureRange': [2500, 9900, 100]}]}
        values = {'getIsoSpeedRate': ['100'], 'getFNumber': ['8'], 'getShutterSpeed': ['1/60'],
                  'getAvailableIsoSpeedRate': ['100', ['100']], 'getAvailableFNumber': ['8', ['8']],
                  'getAvailableShutterSpeed': ['1/60', ['1/60']]}
        return {'result': values.get(method, [])}
    def set_iso(self, value): pass
    def set_aperture(self, value): pass
    def set_shutter(self, value): pass
    def take_picture(self):
        self.shutters += 1
        return ['http://camera/exact-current.jpg']
    def download(self, url, destination):
        self.downloads.append((url, destination))
        if self.fail:
            Path(destination).write_bytes(b'partial')
            raise OSError('current download failed')
        Image.new('RGB', (64, 48), 'green').save(destination)
    def transfer_latest_full_res(self, *args, **kwargs):
        raise AssertionError('newest-card files have no capture identity')


def setup_brain(monkeypatch):
    monkeypatch.setattr(wifi_autopilot.time, 'sleep', lambda _: None)
    monkeypatch.setattr(capture_analysis, 'motion_between_files', lambda *a: {'subject_motion_floor_s': None})
    monkeypatch.setattr(wifi_autopilot.vision_banana, 'analyze', lambda *a: None)
    monkeypatch.setattr(wifi_autopilot.live_verify, 'meter_ev100', lambda *a, **k: (8, 0))
    seen = []
    def recommend(*args, **kwargs):
        seen.append((args, kwargs))
        return SimpleNamespace(iso_label='100', aperture_label='8', shutter_label='1/60', cards=[])
    monkeypatch.setattr(wifi_autopilot.brain, 'recommend_settings', recommend)
    return seen


def test_actual_orchestrator_returns_exact_download_and_dimensions(tmp_path, monkeypatch):
    seen = setup_brain(monkeypatch)
    cam = Camera()
    report = wifi_autopilot.shoot(cam, work_dir=str(tmp_path), full_res_dir=str(tmp_path),
                                 target_brightness=82, white_balance='warm', motion_intent='freeze')
    assert report['target_brightness'] == 82
    assert report['white_balance']['verified'] is True
    assert ('setWhiteBalance', ['Color Temperature', True, 7500]) in cam.calls
    assert seen[0][1]['subject_motion_floor_s'] == 1/500
    assert seen[0][0][1] > 8  # darker requested look changes the metering target
    assert report['postview_downloaded'] is True
    assert report['image_dimensions'] == [64, 48]
    assert cam.downloads == [('http://camera/exact-current.jpg', report['postview_path'])]
    assert Path(report['postview_path']).parent == Path(report['attempt_dir'])
    assert Path(report['attempt_dir']).stat().st_mode & 0o777 == 0o700
    assert report['full_res'] == []
    assert report['output_contract']['full_res_requested'] is True
    assert report['output_contract']['full_res_available'] is False
    assert report['chosen_subject_focus_verified'] is False
    assert report['focus_coordinate_applied'] is False
    assert report['focus_result_source'] == 'not_verified'


def test_failed_download_preserves_receipt_and_cannot_return_stale_frame(tmp_path, monkeypatch):
    setup_brain(monkeypatch)
    stale = tmp_path / 'auto_pv.jpg'
    Image.new('RGB', (64, 48), 'red').save(stale)
    cam = Camera(fail=True)
    with pytest.raises(CaptureOutcomeUncertain, match='current download failed') as caught:
        wifi_autopilot.shoot(cam, frame=str(stale), work_dir=str(tmp_path))
    assert cam.shutters == 1
    assert caught.value.retry_safe is False
    receipt = Path(caught.value.receipt_path)
    assert json.loads(receipt.read_text())['postview_url'] == 'http://camera/exact-current.jpg'
    assert not (receipt.parent / 'postview.jpg').exists()
    assert stale.exists()


def test_download_with_valid_header_but_truncated_pixels_fails(tmp_path, monkeypatch):
    setup_brain(monkeypatch)
    cam = Camera()
    def corrupt(url, destination):
        Image.new('RGB', (64, 48), 'green').save(destination)
        path = Path(destination)
        path.write_bytes(path.read_bytes()[:-50])
    cam.download = corrupt
    with pytest.raises(CaptureOutcomeUncertain) as caught:
        wifi_autopilot.shoot(cam, work_dir=str(tmp_path))
    assert cam.shutters == 1
    assert Path(caught.value.receipt_path).is_file()
    assert not list(tmp_path.glob('*/postview.jpg'))


@pytest.mark.parametrize('failure', ['missing_response', 'download', 'decode', 'copy', 'shutter_error'])
def test_post_shutter_failures_return_409_without_retry(tmp_path, monkeypatch, failure):
    import importlib
    import threading
    from fastapi.testclient import TestClient
    import engine
    import wifi_engine
    setup_brain(monkeypatch)
    cam = Camera(fail=failure == 'download')
    if failure in ('missing_response', 'shutter_error'):
        def failed_take():
            cam.shutters += 1
            if failure == 'shutter_error': raise OSError('shutter response lost')
            return []
        cam.take_picture = failed_take
    if failure == 'decode':
        def invalid_download(url, destination): Path(destination).write_bytes(b'invalid jpeg')
        cam.download = invalid_download
    if failure == 'copy':
        def failed_copy(source): raise OSError('gallery write failed')
        monkeypatch.setattr(wifi_engine, '_save_into_shots', failed_copy)
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path / 'shots')
    monkeypatch.setattr(wifi_engine.WiFiEngine, '_smart_remote_camera', lambda self: cam)
    monkeypatch.setattr(wifi_engine.intent_brain, 'interpret', lambda *a, **k: {
        'target_brightness': 112, 'white_balance': 'auto', 'why': 'fixture'})
    monkeypatch.setenv('CAMERA_ENGINE', 'mock')
    api = importlib.import_module('app')
    monkeypatch.setattr(api, '_selected_engine', lambda request: wifi_engine.WiFiEngine())
    monkeypatch.setattr(api, '_camera_lock', threading.Lock())
    monkeypatch.setattr(api, '_capture_budget', api.RequestBudget(limit=100))
    monkeypatch.setattr(api, 'API_TOKEN', 'wifi-uncertainty-fixture-token')
    response = TestClient(api.app).post('/shoot', json={'refine': True},
        headers={'X-Camera-Autopilot-Token': api.API_TOKEN})
    assert response.status_code == 409, response.text
    assert response.json()['retry_capture'] is False
    assert response.json()['may_have_fired'] is True
    assert cam.shutters == 1
    receipts = list((tmp_path / 'shots').glob('.wifi-capture-*/wifi-attempt-*/capture-receipt.json'))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert receipt['retry_safe'] is False
    if failure not in ('missing_response', 'shutter_error'):
        assert receipt['postview_url'] == 'http://camera/exact-current.jpg'
    if failure == 'copy':
        preserved = Path(response.json()['capture_path'])
        assert preserved.exists()
        with Image.open(preserved) as image: assert image.size == (64, 48)
        assert receipt['state'] == 'postview_verified'
