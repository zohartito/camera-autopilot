import json
import stat
import subprocess
import sys
from pathlib import Path
import pytest
from deploy.portable_bundle import main, render_bundle, write_bundle

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('script', ['deploy/portable_bundle.py', 'deploy/host_readiness.py'])
def test_path_invocation_bootstraps_package_root(script, tmp_path):
    # Run by path (what a person on a fresh board types), from an unrelated cwd:
    # Python puts deploy/ on sys.path, not the repo root, so `import deploy.*` /
    # `import backend.*` raises ModuleNotFoundError unless the script bootstraps the
    # root. Proves the bootstrap, not the cwd, is what makes the path form work.
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / script), '--help'],
        cwd=tmp_path, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert 'ModuleNotFoundError' not in result.stderr


def test_default_dry_run_never_exposes_credentials(capsys):
    assert main([]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['dry_run'] and not report['network_changed']
    assert not report['services_started'] and not report['credentials_printed']


def test_private_files_and_no_silent_credentials_rotation(tmp_path):
    target = tmp_path / 'bundle'
    files = render_bundle()
    report = write_bundle(target, files)
    assert stat.S_IMODE(target.stat().st_mode) == 0o700
    assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in target.iterdir())
    assert not report['installed']
    with pytest.raises(FileExistsError): write_bundle(target, render_bundle())
    assert (target / 'api-token').read_text() == files['api-token']


def test_ap_tls_offline_and_credentials_contract():
    files = render_bundle('cm5-hybrid')
    assert 'autoconnect=false' in files['camera-puck.nmconnection']
    assert 'method=shared' in files['camera-puck.nmconnection']
    assert 'wpa=2' in files['hostapd.conf']
    assert 'bind 192.168.77.1' in files['Caddyfile']
    assert '127.0.0.1:8000' in files['Caddyfile']
    assert 'HF_HUB_OFFLINE=1' in files['runtime.env']
    assert files['api-token'].strip() in files['runtime.env']
    assert not json.loads(files['profile.json'])['ble_enabled']
    assert json.loads(files['offline-artifacts.json'])['artifacts'] == []
    assert 'PrivateDevices=yes' not in files['camera-autopilot.service'].replace('# USB must remain visible; do not add PrivateDevices=yes.', '')


@pytest.mark.parametrize('kwargs', [{'profile':'unknown'}, {'interface':'wlan0\nmalicious=1'}, {'country':'U\n'}, {'repo':Path('relative')}])
def test_invalid_configuration_rejected(kwargs):
    with pytest.raises(ValueError): render_bundle(**kwargs)


@pytest.mark.parametrize('band,channel,nm,hostapd',[
    ('2.4ghz',11,'bg','g'),('5ghz',44,'a','a')])
def test_band_configuration_is_rendered_consistently_without_radio_claims(band,channel,nm,hostapd):
    files=render_bundle(band=band,channel=channel)
    assert f'band={nm}\nchannel={channel}' in files['camera-puck.nmconnection']
    assert f'hw_mode={hostapd}\nchannel={channel}' in files['hostapd.conf']
    profile=json.loads(files['profile.json'])
    assert profile['radio']['band']==band and profile['radio']['channel']==channel
    assert not profile['radio']['hardware_support_verified']
    assert not profile['radio']['runtime_access_point_verified']
    assert profile['device_id'] in files['runtime.env']


@pytest.mark.parametrize('kwargs',[{'band':'auto'},{'band':'6ghz'},{'band':'5ghz','channel':6},
    {'band':'2.4ghz','channel':36},{'channel':True}])
def test_unsupported_band_or_channel_rejected_before_rendering(kwargs):
    with pytest.raises(ValueError):render_bundle(**kwargs)


def test_5ghz_cli_dry_run_reports_configuration_only(capsys):
    assert main(['--band','5ghz','--channel','36'])==0
    report=json.loads(capsys.readouterr().out)
    assert report['dry_run'] and report['radio']['band']=='5ghz'
    assert not report['network_changed'] and not report['radio']['regulatory_support_verified']
