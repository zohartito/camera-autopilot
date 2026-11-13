"""Disabled legacy entry points cannot reach a camera or materialize files."""
from __future__ import annotations

import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))


@pytest.mark.parametrize('module_name,helper,flags', [
    ('prove_openmemories_endpoint_surface', 'capture_surface', ['--include-actions', '--allow-camera-actions']),
    ('prove_openmemories_param_set_read_revert', 'run_proof', ['--setting-id', 'param.iso_sensitivity', '--value', '640', '--execute']),
])
def test_route3_cli_fails_before_any_helper_or_transport(module_name, helper, flags, monkeypatch, capsys, tmp_path):
    module = importlib.import_module(module_name)

    def forbidden(*args, **kwargs):
        raise AssertionError('disabled CLI reached camera protocol helper')

    monkeypatch.setattr(module, helper, forbidden)
    monkeypatch.setattr(module.urllib.request, 'urlopen', forbidden)
    out = tmp_path / 'must-not-exist'
    args = ['--base', 'http://127.0.0.1:1', '--out-dir', str(out), *flags]
    assert module.main(args) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok'] is False and payload['executed'] is False
    assert 'permanently disabled' in payload['error']
    assert not out.exists()
    # Also exercise the actual executable path, without shell interpolation.
    result = subprocess.run([sys.executable, str(ROOT / 'tools' / f'{module_name}.py'), *args], capture_output=True, text=True, timeout=5)
    assert result.returncode == 2
    assert json.loads(result.stdout)['executed'] is False
    assert not out.exists()


def test_bridge_preview_returns_gone_without_subprocess_or_artifact(tmp_path, monkeypatch):
    import usb_ptp_bridge_server as bridge
    import types

    def forbidden(*args, **kwargs):
        raise AssertionError('disabled preview attempted to create a file or invoke gphoto')

    state = types.SimpleNamespace(request_dir=forbidden)
    handler = object.__new__(bridge.Handler)
    handler.server = types.SimpleNamespace(state=state)
    responses = []
    handler._json = lambda status, payload: responses.append((status.value, payload))
    monkeypatch.setattr(bridge, '_run', forbidden)
    handler._handle_preview()
    assert responses[0][0] == 410
    assert responses[0][1]['ok'] is False
    assert 'preview is disabled' in responses[0][1]['error']
    assert list(tmp_path.iterdir()) == []
