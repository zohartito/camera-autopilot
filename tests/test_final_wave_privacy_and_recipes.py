import os
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('mock', [False, True])
def test_gallery_is_private_even_with_permissive_umask(monkeypatch, tmp_path, mock):
    import engine
    source = tmp_path / 'source.jpg'
    source.write_bytes(b'camera JPEG bytes')
    root = tmp_path / 'gallery'
    root.mkdir(mode=0o755)
    monkeypatch.setattr(engine, 'SHOTS_DIR', root)
    previous = os.umask(0)
    try:
        name = engine._save_into_shots(None if mock else source)
    finally:
        os.umask(previous)
    assert root.stat().st_mode & 0o777 == 0o700
    assert (root / name).stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('requested', [None, 'warm'])
def test_result_recipe_does_not_claim_requested_white_balance(monkeypatch, tmp_path, requested):
    import engine
    import product_capture
    monkeypatch.setattr(engine, 'SHOTS_DIR', tmp_path)
    monkeypatch.setattr(product_capture.intent_brain, 'interpret',
                        lambda _: {'target_brightness': 112, 'white_balance': requested})
    def capture(*args, **kwargs):
        (tmp_path / 'shot_real.jpg').write_bytes(b'new captured bytes')
        return engine.Shot(ok=True, image='shot_real.jpg', summary='Photo captured', why='Review it.',
            mood={'settings': {'white_balance': 'auto'}}, settings={'white_balance': 'Daylight'},
            applied={'white_balance': {'requested': requested, 'verified': False}})
    shot = product_capture.shoot(SimpleNamespace(name='real', shoot=capture), 'natural')
    assert shot.mood['status'] == 'requested_recipe'
    assert shot.mood['outcome_verified'] is False
    assert shot.mood['settings']['white_balance'] == (requested or 'unchanged')
    assert shot.mood['observed_settings']['white_balance'] == 'Daylight'
    if requested:
        assert 'not verified' in shot.why
