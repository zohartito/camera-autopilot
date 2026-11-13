"""Camera-free proof of capture identity and intent propagation through Wi-Fi."""
from pathlib import Path
import types
import sys

import pytest
from PIL import Image
import engine
import wifi_engine
from sonyremote import CaptureOutcomeUncertain


def test_shoot_uses_current_postview_and_preserves_exact_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path / 'shots')
    calls = []
    def capture(cam, **kwargs):
        calls.append(kwargs)
        attempt = Path(kwargs['work_dir']) / 'wifi-attempt-current'
        attempt.mkdir(mode=0o700)
        source = attempt / 'postview.jpg'
        Image.new('RGB', (48, 32), 'red').save(source)
        return {'attempt_dir': str(attempt), 'capture_id': attempt.name,
                'postview_path': str(source), 'postview_downloaded': True,
                'postview': ['http://camera/current.jpg'], 'image_dimensions': [48, 32],
                'image_kind': 'postview', 'full_res': [],
                'white_balance': {'requested': 'warm', 'verified': True}}
    monkeypatch.setitem(sys.modules, 'wifi_autopilot', types.SimpleNamespace(shoot=capture))
    monkeypatch.setattr(wifi_engine.intent_brain, 'interpret', lambda *a, **k: {
        'target_brightness': 82, 'white_balance': 'warm', 'motion_intent': 'freeze', 'why': 'Exact recipe'})
    monkeypatch.setattr(wifi_engine.WiFiEngine, '_smart_remote_camera', lambda self: object())
    result = wifi_engine.WiFiEngine().shoot('warm', target_override=91, variant_id='noir')
    assert result.ok
    assert calls[0]['target_brightness'] == 91
    assert calls[0]['white_balance'] == 'warm'
    assert calls[0]['motion_intent'] == 'freeze'
    assert 'full_res_dir' not in calls[0]
    assert result.evidence['image_kind'] == 'postview'
    assert result.evidence['image_dimensions'] == [48, 32]
    assert result.applied['variant_id'] == 'noir'
    assert (engine.SHOTS_DIR / result.image).is_file()
    assert result.settings['image_source'] == str(engine.SHOTS_DIR / result.image)
    assert not Path(calls[0]['work_dir']).exists()


def test_no_global_or_newest_card_fallback_on_failed_current_download(tmp_path, monkeypatch):
    stale = tmp_path / 'auto_pv.jpg'
    Image.new('RGB', (8, 8), 'blue').save(stale)
    report = {'postview': ['http://camera/new.jpg'], 'full_res': [str(stale)],
              'image': str(stale), 'final_image': str(stale), 'postview_downloaded': False}
    assert wifi_engine._best_image_source(report) is None
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path / 'shots')
    monkeypatch.setitem(sys.modules, 'wifi_autopilot', types.SimpleNamespace(shoot=lambda *a, **k: report))
    monkeypatch.setattr(wifi_engine.WiFiEngine, '_smart_remote_camera', lambda self: object())
    monkeypatch.setattr(wifi_engine.intent_brain, 'interpret', lambda *a, **k: {
        'target_brightness': 112, 'white_balance': 'auto', 'why': ''})
    with pytest.raises(CaptureOutcomeUncertain, match='no verified captured image'):
        wifi_engine.WiFiEngine().shoot(None)
    assert len(list(engine.SHOTS_DIR.glob('.wifi-capture-*/publication-receipt.json'))) == 1


def test_previous_attempt_cannot_be_returned_for_current_capture(tmp_path):
    old = tmp_path / 'old' / 'wifi-attempt-old'
    old.mkdir(parents=True)
    source = old / 'postview.jpg'
    Image.new('RGB', (8, 8)).save(source)
    current = tmp_path / 'current'
    current.mkdir()
    report = {'attempt_dir': str(old), 'capture_id': old.name,
              'postview_path': str(source), 'postview_downloaded': True}
    assert wifi_engine._best_image_source(report, current) is None


def _postview_capture(kwargs):
    """A camera-free wifi_autopilot.shoot double: writes this capture's real postview and returns
    the standard report (with a `postview` URL so the engine will consider a full-res pull)."""
    attempt = Path(kwargs['work_dir']) / 'wifi-attempt-current'
    attempt.mkdir(mode=0o700)
    source = attempt / 'postview.jpg'
    Image.new('RGB', (48, 32), 'red').save(source)
    return {'attempt_dir': str(attempt), 'capture_id': attempt.name,
            'postview_path': str(source), 'postview_downloaded': True,
            'postview': ['http://camera/current.jpg'], 'image_dimensions': [48, 32],
            'image_kind': 'postview', 'full_res': [], 'full_res_status': 'postview_only',
            'white_balance': {'requested': 'auto', 'verified': True}}


def test_shoot_full_res_on_publishes_verified_full_res_image(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path / 'shots')
    monkeypatch.setitem(sys.modules, 'wifi_autopilot',
                        types.SimpleNamespace(shoot=lambda cam, **kw: _postview_capture(kw)))
    monkeypatch.setattr(wifi_engine.WiFiEngine, '_smart_remote_camera', lambda self: object())
    monkeypatch.setattr(wifi_engine.intent_brain, 'interpret', lambda *a, **k: {
        'target_brightness': 112, 'white_balance': 'auto', 'why': ''})

    def fake_pull(cam, postview_path, work_dir, *, shutter_time, count=3):
        full = Path(work_dir) / 'DSC_full.jpg'
        Image.new('RGB', (120, 80), 'green').save(full)
        from stack_session import frame_receipt
        return {'status': 'verified', 'path': str(full), 'receipt': frame_receipt(full),
                'identity': {'identity_basis': 'content_only', 'ncc': 0.99, 'phash_distance': 0,
                             'verified': True, 'reason': 'ok'},
                'identity_basis': 'content_only', 'candidates_considered': 1, 'session_resumed': True}
    monkeypatch.setattr(wifi_engine.wifi_full_res, 'pull_full_res_for_capture', fake_pull)

    result = wifi_engine.WiFiEngine(full_res=True).shoot('a room')
    assert result.ok
    assert result.evidence['image_kind'] == 'full_res'
    assert result.evidence['image_dimensions'] == [120, 80]
    assert result.evidence['full_res_status'] == 'verified_by_content'
    assert result.evidence['full_res']['receipt']['width'] == 120
    saved = engine.SHOTS_DIR / result.image
    assert saved.is_file()
    with Image.open(saved) as published:
        assert published.size == (120, 80)          # the 24 MP stand-in, not the 48x32 postview


def test_shoot_full_res_off_publishes_postview_and_never_pulls(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path / 'shots')
    monkeypatch.setitem(sys.modules, 'wifi_autopilot',
                        types.SimpleNamespace(shoot=lambda cam, **kw: _postview_capture(kw)))
    monkeypatch.setattr(wifi_engine.WiFiEngine, '_smart_remote_camera', lambda self: object())
    monkeypatch.setattr(wifi_engine.intent_brain, 'interpret', lambda *a, **k: {
        'target_brightness': 112, 'white_balance': 'auto', 'why': ''})

    def forbidden_pull(*a, **k):
        raise AssertionError('full-res pull must not run when full_res is off')
    monkeypatch.setattr(wifi_engine.wifi_full_res, 'pull_full_res_for_capture', forbidden_pull)

    result = wifi_engine.WiFiEngine(full_res=False).shoot('a room')
    assert result.ok
    assert result.evidence['image_kind'] == 'postview'
    assert result.evidence['image_dimensions'] == [48, 32]
    with Image.open(engine.SHOTS_DIR / result.image) as published:
        assert published.size == (48, 32)
